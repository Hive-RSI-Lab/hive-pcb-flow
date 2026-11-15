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
 - mcu_pwm_pair (warning): a gate driver's high-side/low-side input pairs
   (reference/mcu_pins.yaml loads with pwm_pairs: a DRV8300's INHx/INLx)
   fed from one MCU on pins that can't give each pair one channel's CHx
   and CHxN of a single timer: the timer's dead-time generator and break
   input can't keep that leg's two switches apart, and the legs don't share
   one counter (#1115).
 - mcu_input_only_drive (warning): a chip-select input (~CS, nCS, CS#)
   joined, directly or over 0 ohm, to an MCU pin mcu_pins.yaml lists as
   input-only (#1032, glasgow-10: flash select on the FX2's RDY0).
 - i2c_address_collision (error): two devices on one SCL net whose
   address straps (reference/i2c_addresses.yaml) give the same 7-bit
   address (#1034, glasgow-2). A strap it can't read leaves that device out;
   a fixed-address part (no strap) counts only when its SCL net is plainly
   a bus (another part on it, no pull to ground) (#1105).
 - bus_cap_load (SD: error; I2C: warning past the 400 pF budget, error
   past 550 pF): the capacitance the board puts on one bus line, from
   reference/bus_load.yaml's parts (ESD clamps) and each capacitor from
   the line to ground or a rail, over the line's budget (#1031,
   echoplayer-3: CESD3V3AP clamps on every SDMMC line). SD lines are the
   nets on a card socket's CLK/CMD/DAT pins; I2C lines are SDA/SCL pins.
   The sum leaves out the host, the card and the copper, so it is a
   floor. A part marked DNP is not fitted and adds nothing.
 - pull_vs_leakage: a signal net (0 ohm links followed) pulled to a rail
   of known volts by resistors too weak for the pins on it that
   reference/pin_leakage.yaml says sink or source current before they are
   set up (an unconfigured FPGA's weak pull-downs, input leakage) (#1026).
   The worst-case level is the pulls' Thevenin source less that current
   through their resistance; error when a listed receiver reads it as the
   wrong state, warning when it sits between VIL and VIH (receivers not
   listed: 0.3 / 0.7 of the rail). A row's vil_frac / vih_frac and
   internal pull-up refer to the part's own supply pin, not the pull's
   rail (#1104). A net with a push-pull output is skipped,
   and so is a level the pulls alone already put in that band (a divider
   into an ADC input sits mid-rail by design, #1100).
 - driver_contention (warning): a reset input pin_leakage.yaml lists that
   shares its net with output, bidirectional or tri-state pins of two or
   more other parts (connectors and open-collector pins aside): they can
   fight on the line (#1026).
 - io_overvoltage (error): an output of a reference/io_levels.yaml driver
   whose guaranteed minimum high (its supply rail less voh_drop) reaches,
   directly or through series resistors (any divider to a known rail or
   ground solved), an I/O pin of a listed receiver above its vin_max: a 5 V
   part into a 3.3 V FPGA through dropping resistors only (#1029).
 The datasheet-table rules (pin_state through io_overvoltage) count a row
   only once its part says verified: true. A finding that needs a drafted,
   unverified row (scripts/part_rows.py drafts them) is listed under
   facts' not_judged, counted as unverified_findings, and is not a hit (#1100).
 - power_good_unused (warning): an IC's power-good output (PG, PGOOD,
   PWRGD, POK...) wired to its net but read by nothing there or one series
   resistor on: only resistors, capacitors, LEDs, test points and jumpers.
   Whatever the converter powers starts before its output is in regulation
   (#1030). A PG left unconnected is a choice and is not flagged.
 - power_path_body_diode (warning): a MOSFET between a battery's positive
   net (BAT/VBAT/BATT/+BATT/CELL by name, or a BT* part's + pin) and a
   rail with a source of its own (a power_out pin, a connector, a VBUS /
   VIN / USB / 5V name) whose body diode (N: source to drain, P: drain to
   source) points into the battery: with the FET off, that rail charges
   the cell through the diode (#1085; TI SLVA139, Microchip AN1149). A
   back-to-back pair's middle node is no rail and is skipped.
 - power_switch_reversed (warning): a MOSFET off the battery nets whose
   body diode conducts the way the switch should block: a gate-driven
   (a transistor's drain, or an IC GPIO, on the gate) high-side switch
   with a P-FET's drain or an N-FET's source on the sourced rail; a
   gate-driven N-FET with its drain, not its source, on ground; a P-FET
   with its gate tied to ground (reverse-polarity protection) and its
   source, not its drain, on the input (#1085; TI SLVA139). A gate from
   a controller IC's gate-drive pin (ideal-diode, hot-swap), a gate held
   by a rail, both sides sourced or neither, negative rails, and a FET
   whose value, symbol and description name no channel are not
   judged and counted in power_switches_not_judged.
 - protector_orientation (warning): a single-cell protector with S-8261's
   pins (VDD, VSS, VM, DO, CO) whose VSS net (the cell's -) carries more
   other ICs' supply pins than the pack - net its VM resistor reaches:
   the load returns around the protection FETs (#1085; ABLIC S-8261).
 - offboard_input_weak_pull (warning): a connector pin whose net is held
   only by a resistor above 10k to a rail or ground, with no on-board driver
   and no TVS or ESD clamp on it, read by an IC input or a transistor's base
   or gate there or one series resistor on: the line floats with the cable
   out and picks up a long wire (TI SPRACK9: 4.7k-10k on a line that may be
   disconnected; Microchip AN2587: TVS on external connector I/O) (#1079).
 - symbol_value_mismatch (warning): a part whose symbol name carries a size
   (0.2in, 12mm) that its Value, or failing that its footprint name,
   contradicts with no size in common: another part's symbol placed, so
   its pin map need not be this part's (#1086; netscore-big's 0.8in digits
   on a '7Seg 0.2in' symbol). Connectors and 2-pin parts are skipped.
 - symbol_pinmap (warning): a part whose Value or MPN names a KiCad stock
   symbol (exact name, an x-wildcard name such as LM2596S-xx, or the same
   74-series function) in the same package family, and whose own symbol
   puts a pin name both carry (IN, OUT, GND, Q0, ...) on different pin
   numbers than every such stock symbol does: a pin map from another
   package variant or part (#1086, miss class 5). A part with no stock
   symbol in its package is compared with its datasheet pin table in
   reference/part_pinouts.yaml (matched on name and footprint, at least 3
   names in common; an unverified row's finding is not judged) (#1135).
   A stock symbol's package is its default footprint, else its
   ki_fp_filters: a PLCC-44 part is not compared with a DIP-40-numbered
   stock symbol, and a footprint naming no package among stock symbols of
   several packages is not judged (#1203). Needs the stock symbol dir
   (lib/env.kicad_symbol_dir); not judged without it.
 - bus_order_flipped (warning): two units of one part with channel pins of
   one stem (1QA..1QD, 2QA..2QD) whose nets carry one varying number that
   rises along one unit and falls along the other: one unit's pins drawn
   in reverse (gcm v5.0's SN74LV393A).
 - mcu_i2c_role (warning): an MCU whose reference/mcu_pins.yaml row maps its
   pins to I2C blocks, with one block's SDA pin on the board's SCL net and
   its SCL pin on the SDA net (a net's role is its name, else an SDA/SCL
   pin of another part on it): the hardware block can't drive the bus
   unless firmware bit-bangs it (pico-if1 v1.3's OLED). An unverified row's
   finding is not judged (#1100).
 - offboard_input_clamp / offboard_input_rail_absent / offboard_input_over
   (error), offboard_input_unprotected / offboard_input_floating (warning):
   every IC pin on a connector net, or one 2-pin resistor from one or from
   an off-board rail (a rail on a connector no on-board power pin is on),
   judged against its row in reference/pin_abs_max.yaml (abs-max, negative
   included, rows drafted and cited by part_rows.py). A resistor-only
   solve gives the pin's level with every rail present and with each
   off-board rail gone; a listed silicon clamp diode reaches rail -/+ its
   vf_max. clamp: the diode that holds the pin lets it past its rating
   (zxdiag-v11's 1N4148 to -1 V on an RP2040 ADC rated -0.5 V); rail_absent:
   with the far system's rail gone the divider takes the pin past it;
   over: past it with every rail up; unprotected: a cable (not a header or
   board-to-board part) that also carries a rail above the pin's rating,
   wired straight to a pin with 0.5 V or less of margin, no clamp and no
   series resistor (smart-lcc's J1 with +12V); floating: a status-named
   net (STATE, DET, INT...) read by an IC input with no pull, driver or
   clamp, on a cable that brings a rail above the reader's supply and is
   not a host-bus edge (fc980c's KEY_STATE); an info note instead when
   every reader's pin is rated below that rail (glasgow's ECP5 DETECT on a
   +5V port): the line can only idle at the reader's own rail, a pull set
   in gateware or firmware. Fact offboard_inputs_judged
   counts the (net, pin) pairs a verified row covered (class 3 of
   docs/miss-taxonomy.md).
 - pin_role_net (error/warning/info): a part's pin whose role, by its
   symbol's pin name, doesn't fit its net: a ground pin (GND, VSS) on a
   positive rail is an error, a supply pin (VCC, VDD, VBUS, VIN) on ground
   a warning, a VBAT pin on ground an info note (an unused RTC backup
   domain). One finding per part; connectors, test points and jumpers
   are not judged (flexgrid v4's USBLC6-2SC6 GND on VBUS).
 - protector_cell_side (warning): a battery protector with a cell-side
   pin (GND/VSS) on the board's ground and a load-side pin (BAT-, VM, P-,
   EB-) on the cell's own terminal: the sides swapped (netscore's
   FS312MH).
 - debug_pin_wrong (error), debug_pins_reused (warning): an SWD line, by
   its net's or header pin's name, reaching an MCU of reference/
   mcu_pins.yaml's debug rows on a pin other than its SWDIO/SWCLK pin
   (fc980c's SWDIO on the F401's PA15); an SWD pin wired to other parts
   with no header, test point or SWD-named net within one resistor (and
   on STM32G0/C0 PA14 is BOOT0 too). Class 6 of docs/miss-taxonomy.md;
   facts pin_role_parts_checked, protector_cell_sides_checked,
   debug_mcus_checked.
 - rail_over / ldo_heat / gpio_over (error), gpio_total_over / vbus_over
   (warning; vbus_over an error on a port with no CC pins): a load against
   its source's limit, from reference/source_limits.yaml rows (cited,
   verified by part_rows.py). rail_over: the loads a row names on a
   regulator's output (through 0 ohm links; a downstream LDO passes its
   own load up; a resistor + LED to ground whose value gives its Vf) ask
   more than its iout_max (flexgrid's 200 mA TPS7A03 feeding an
   ESP32-S3-WROOM-1 that wants a 500 mA supply). ldo_heat: (Vin - Vout)
   x the continuous load (a row marked peak, a radio's burst, is not
   heat) x its best package's theta_ja + 25 degC past tj_max. gpio_over:
   an MCU pin driving or sinking an LED through a resistor past pin_max;
   the pins together past total_max warn. vbus_over: a USB connector's
   VBUS loads past 500 mA (900 mA with SuperSpeed pins) and no
   power-delivery controller on the board. A rail with loads no row
   covers, an LED with no Vf in its value, or volts the net names don't
   give is not judged, never guessed. Class 11 of docs/miss-taxonomy.md;
   fact source_limits_checked.
 - switched_load_droop (error): a capacitive load the constraints declare
   (`switched_loads: [{nets: [a, b] | net: x, capacitance_f}]`, as no
   schematic carries a panel's or cable's capacitance) that a FET switches
   hard from a converter's output. The rail's volts come from the
   constraints or the converter's FB divider (converters rows: vref,
   uv_pct, ilim_min, hiccup_cycles); each edge moves C_load x the swing
   (V on one leg, 2V across a bridge's two legs), and the output
   capacitors fall C_load x swing / (C_out + C_load), an error past the
   converter's undervoltage threshold unless a series resistor holds the
   edge current under ilim_min (pdlc v1: 12.3 uF of panel on a bare
   bridge from an LM5158's 105 uF at 61 V falls 21%; it hiccups). A load
   fed through an inductor, or a rail no converters row covers, is not
   judged. Fact switched_loads_checked.
 - polarity_mismatch (error; a warning for interrupts and for a reset or
   power-good into an enable), polarity_net_name (warning),
   idle_pull_asserted (error): a
   control signal's active level across a connection (a reset output
   asserting low into a reset input asserting high, directly, through a
   0 ohm rename or a translator channel; a net named active-low on pins
   that assert high; a reset held asserted by its only pull). Class 14
   of the gaps list; lib/signal_domains.py; fact polarity_pins_checked.
 - io_domain_overdrive (error), io_domain_underdrive (warning),
   io_domain_pull_rail (error over, warning under): an output on one
   rail into an input whose datasheet row (reference/io_tolerance.yaml,
   pin_abs_max.yaml) takes less, a high below the receiver's VIH, an
   open-drain pull above or below a receiver's limits; a receiver with
   no row is not_judged. Class 15 of the gaps list; fact
   io_domain_pairs_checked.
 - pair_polarity_swap (error for USB 2.0 and LVDS, else a warning),
   pair_split (error), pair_lane_order (error): a differential line's
   P/N, or an LVDS lane's place, across a connector against the
   connector's stated pinout (constraints connector_pinouts, its pin
   names, or the USB / Type-C / 8P8C standard pinout for its footprint),
   followed through series parts to the first part naming the pair. A
   common-mode choke or transformer that names no pins, an Ethernet
   crossover, and a chip pair on a connector nothing names are
   not_judged. lib/pair_order.py (#1217); fact connector_pairs_checked.
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
import itertools
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
import pinmaplib  # noqa: E402

SCRIPT = "netlist_audit"
SOURCE = "audit.netlist"
PIN_REQUIREMENTS = HERE.parent / "reference" / "pin_requirements.yaml"
PIN_STATES = HERE.parent / "reference" / "pin_states.yaml"
MCU_PINS = HERE.parent / "reference" / "mcu_pins.yaml"
IO_SUPPLY = HERE.parent / "reference" / "io_supply.yaml"
I2C_ADDRESSES = HERE.parent / "reference" / "i2c_addresses.yaml"
BUS_LOAD = HERE.parent / "reference" / "bus_load.yaml"
PIN_LEAKAGE = HERE.parent / "reference" / "pin_leakage.yaml"
IO_LEVELS = HERE.parent / "reference" / "io_levels.yaml"
PART_PINOUTS = HERE.parent / "reference" / "part_pinouts.yaml"

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


# a Value or field that marks a part not fitted: DNP / DNF / NF / NoPop /
# "do not populate|fit|place" / "not fitted" as a word of its own
_DNP_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:DNP|DNF|DNI|NoPop|No[ _-]Pop(?:ulate)?|"
    r"do[ _-]?not[ _-]?(?:populate|fit|place|mount)|not[ _-]fitted)"
    r"(?![A-Za-z0-9])", re.I)
_DNP_FIELDS = {"DNP", "DNF", "DONOTPOPULATE", "NOPOP"}
_TRUE = {"1", "yes", "y", "true", "x", "dnp", "dnf"}


def _field_dnp(fname: str, val: str) -> bool:
    """A schematic field that marks its part not fitted: a DNP-named field
    set true, a Populate/Fitted field set false, or any field (not a
    description) whose whole value is a DNP marker."""
    key, v = fname.replace("_", "").upper(), val.strip()
    if key in _DNP_FIELDS:
        return v.lower() in _TRUE
    if key in {"POPULATE", "FITTED", "FIT", "MOUNT"}:
        return v.lower() in {"0", "no", "n", "false", "dnp", "dnf"}
    return bool(v) and _DNP_RE.fullmatch(v) is not None


_MPN_FIELDS = {"MPN", "MANUFACTURERPARTNUMBER", "PARTNUMBER", "MFRPN",
               "MFR_PN", "MANF#"}


def parse_netlist(path: Path | str) -> dict:
    """kicadsexpr netlist -> {"components": {ref: {value, footprint,
    libsource?, unit_pins?, mpn?}}, "nets": {name: [{ref, pin, pintype}]},
    "libparts": {(lib, part): set(pin numbers)}, "libpart_pins": {(lib,
    part): {pin number: pin name}}}. Multiline or single-line
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
            # KiCad writes do-not-populate as a valueless
            # `(property (name "dnp"))`, present only when set
            if any(_atom(pr, "name") == "dnp" for pr in _kids(c, "property")):
                comps[ref]["dnp"] = True
            # ...or older schematics write it into the Value ("1x4 THT
            # (DNP)") or a field ("DNP" = yes, Populate = DNF)
            if _DNP_RE.search(comps[ref]["value"] or ""):
                comps[ref]["dnp"] = True
            # the part number, from the fields any of MPN / Manufacturer
            # Part Number / PartNumber (pin-map check looks the part up)
            for fb in _kids(c, "fields"):
                for fld in _kids(fb, "field"):
                    fname = (_atom(fld, "name") or "").replace(" ", "")
                    val = fld[-1] if len(fld) > 1 else None
                    if fname.upper() in _MPN_FIELDS and \
                            not isinstance(val, list) and str(val).strip():
                        comps[ref].setdefault("mpn", str(val).strip())
                    if not isinstance(val, list) and _field_dnp(
                            fname, str(val) if val is not None else ""):
                        comps[ref]["dnp"] = True
            sp = _kids(c, "sheetpath")
            if sp and _atom(sp[0], "names"):
                comps[ref]["sheet"] = _atom(sp[0], "names")
            src = _kids(c, "libsource")
            if src:
                comps[ref]["libsource"] = (_atom(src[0], "lib") or "",
                                           _atom(src[0], "part") or "")
                if _atom(src[0], "description"):
                    comps[ref]["libdesc"] = _atom(src[0], "description")
            unit_pins: set[str] = set()
            for ub in _kids(c, "units"):
                for u in _kids(ub, "unit"):
                    unit_pins |= _pin_nums(u)
            if unit_pins:
                comps[ref]["unit_pins"] = sorted(unit_pins)
    libparts: dict[tuple, set] = {}
    libpart_pins: dict[tuple, dict] = {}
    for blk in _kids(data, "libparts"):
        for lp in _kids(blk, "libpart"):
            key = (_atom(lp, "lib") or "", _atom(lp, "part") or "")
            libparts.setdefault(key, set()).update(_pin_nums(lp))
            names = libpart_pins.setdefault(key, {})
            for pblk in _kids(lp, "pins"):
                for pin in _kids(pblk, "pin"):
                    num = _atom(pin, "num")
                    if num:
                        names.setdefault(num, _atom(pin, "name", ""))
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
    return {"components": comps, "nets": nets, "libparts": libparts,
            "libpart_pins": libpart_pins}


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
    tail = _strip_bus_number(re.split(r"[/.]", name.upper())[-1])
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


def _driven(nets: dict, names: set[str], ref: str) -> bool:
    """A pin of a part other than ref and the two-pin passives (R, C, L)
    is on one of these nets: something else sets its level."""
    return any(m["ref"] != ref and _part_kind(m["ref"]) is None
               for n in names for m in nets.get(n, []))


def _linked_nets(names: set[str], nets: dict, comps: dict,
                 comp_pins: dict[str, dict[str, str]],
                 hops: int = 2) -> set[str]:
    """names plus the nets a fitted 0-ohm resistor or a net tie joins to
    them, up to `hops` links away: a cap behind a 0R link sits on the same
    copper as far as the pin is concerned."""
    seen, edge = set(names), set(names)
    for _ in range(hops):
        nxt = set()
        for n in edge:
            for m in nets.get(n, []):
                r = m["ref"]
                pins = comp_pins.get(r, {})
                if len(pins) != 2 or (comps.get(r) or {}).get("dnp"):
                    continue
                if not (re.match(r"^NT\d", r) or (
                        _part_kind(r) == "R" and r.startswith("R")
                        and engcalc.parse_value(
                            (comps.get(r) or {}).get("value")) == 0)):
                    continue
                nxt |= {f for f in pins.values()
                        if f not in seen and not f.startswith("unconnected-")}
        seen |= nxt
        edge = nxt
        if not edge:
            break
    return seen


def _summed(req: dict) -> bool:
    """A capacitance floor (a min, capacitors only, no nominal value) is the
    pin's total: parallel parts on its net add up (LMR33630 9.2.2.6 "a
    minimum of 10 uF of ceramic capacitance"), so it is judged per net, not
    per part. A max stays per part: a cap over it is still off-value."""
    return (req.get("min") is not None and req.get("value") is None
            and list(req["kinds"]) == ["C"])


def _pin_requirements(nets: dict, comps: dict,
                      comp_pins: dict[str, dict[str, str]],
                      specs: list[dict]):
    """Yield (severity, net, ic, req, found, elsewhere, off_value, unread) per
    datasheet-required pin part that is missing or not drawn with its IC;
    found is None when a `distinct` pair of pins is tied into one net."""
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
                if not a and req.get("when") in ("connected", "undriven"):
                    continue
                if req.get("when") == "undriven" and _driven(nets, a, ref):
                    continue  # another part drives the pin (REFIN fed a ref)
                if a & b:
                    if req.get("distinct"):
                        # tied straight across: the part it needs is not there
                        yield ("error", sorted(a & b)[0], ref, req, None, [],
                               [], [])
                    continue  # the two pins are one net: nothing to span
                lo, hi = _req_window(req)
                found, off, unread = [], [], []
                # a part behind a 0R link or net tie spans the pins too
                la = _linked_nets(a, nets, comps, comp_pins)
                lb = _linked_nets(b, nets, comps, comp_pins)
                if la & lb:
                    la, lb = a, b  # a link joins the two pins: none followed
                vals: dict[str, float] = {}
                for other, pins in sorted(comp_pins.items()):
                    if other == ref or _part_kind(other) not in req["kinds"]:
                        continue
                    if (comps.get(other) or {}).get("dnp"):
                        continue  # not fitted: adds nothing
                    if not (any(n in la for n in pins.values())
                            and any(n in lb for n in pins.values())):
                        continue
                    val = engcalc.part_value(comps.get(other, {}).get("value"),
                                             _part_kind(other))
                    if (lo is not None or hi is not None) and val is None:
                        unread.append(other)  # no value to judge it by
                        continue
                    if val is not None and (hi is None or val <= hi * 1.001):
                        vals[other] = val  # over a max: off, adds nothing
                    if (lo is not None and val < lo * 0.999) or (
                            hi is not None and val > hi * 1.001):
                        off.append(other)
                        continue
                    found.append(other)
                if _summed(req) and not found and len(vals) > 1:
                    # parallel caps add: the floor is the net's total
                    if sum(vals.values()) >= lo * 0.999:
                        found, off = sorted(vals), []
                    else:
                        req = {**req, "_sum": sum(vals.values()),
                               "_sum_of": sorted(vals)}
                net = sorted(a)[0] if a else None
                if not found:
                    yield (req.get("severity", "error"), net, ref, req, [],
                           [], off, unread)
                    continue
                sheet = comp.get("sheet")
                if hierarchical and sheet and not any(
                        comps.get(f, {}).get("sheet") == sheet for f in found):
                    yield "warning", net, ref, req, [], found, off, []


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
                    if any(m["ref"] != ref and _base_type(m.get(
                            "pintype")) == "output"
                           for m in nets.get(name, [])):
                        # a push-pull output holds the net; a pull beside
                        # it only loads the driver (#1228)
                        forbid -= {"pulled_up", "pulled_down"}
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


def load_part_pinouts(path: Path = PART_PINOUTS) -> list[dict]:
    """reference/part_pinouts.yaml's parts (its header says the form)."""
    return _load_yaml(path, "part pinouts").get("parts") or []


def load_i2c_addresses(path: Path = I2C_ADDRESSES) -> list[dict]:
    """reference/i2c_addresses.yaml's devices (its header says the form)."""
    return _load_yaml(path, "i2c addresses").get("devices") or []


def load_bus_load(path: Path = BUS_LOAD) -> dict:
    """reference/bus_load.yaml (its header says the form)."""
    return _load_yaml(path, "bus load")


_SD_SOCKET_PINS = {"CLK", "CMD", "DAT0"}
_SD_PIN_RE = re.compile(r"^(CLK|CMD|DAT[0-7])$")


def _bus_lines(nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]]
               ) -> dict[str, tuple[str, set[str]]]:
    """{net: (bus kind, refs left out of the sum)} for every SD line (a
    net on a card socket's CLK/CMD/DATn pin; the socket is the card's, not
    the board's load) and every I2C line (a net on an SDA or SCL pin)."""
    lines: dict[str, tuple[str, set[str]]] = {}
    for ref in sorted(comps):
        funcs = {_pin_base(f): pn
                 for f, pn in _pin_funcs(ref, nets, comp_pins).items()}
        if _SD_SOCKET_PINS <= set(funcs):
            for base, (_, net) in funcs.items():
                if _SD_PIN_RE.match(base):
                    lines.setdefault(net, ("sd", set()))[1].add(ref)
    for ref in sorted(comps):
        for f, (_, net) in _pin_funcs(ref, nets, comp_pins).items():
            if _pin_base(f) in ("SDA", "SCL") and net not in lines:
                lines[net] = ("i2c", set())
    return {n: k for n, k in lines.items()
            if not n.startswith("unconnected-")}


def _line_load(net: str, skip: set[str], nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]], rails: dict,
               parts: list[dict]) -> tuple[float, list[tuple]]:
    """(pF, [(ref, pF, why)]) that the parts on NET put on it: a
    bus_load.yaml row's c_pf for each of its pins on the net, and each
    two-pin capacitor whose other side is ground or a rail."""
    total, who = 0.0, []
    for ref in sorted({m["ref"] for m in nets[net]} - skip):
        comp = comps.get(ref) or {}
        if comp.get("dnp"):
            continue
        n_on = sum(1 for m in nets[net] if m["ref"] == ref)
        row = next((r for r in parts if _spec_matches(r, comp)), None)
        if row:
            pf = float(row["c_pf"]) * n_on
            total += pf
            who.append((ref, pf, row))
            continue
        pins = comp_pins.get(ref, {})
        if _part_kind(ref) != "C" or len(pins) != 2:
            continue
        other = [n for n in pins.values() if n != net]
        if len(other) != 1:
            continue
        o = other[0]
        if not (_is_ground_name(o) or _supply_net(o, nets.get(o, []), rails)):
            continue
        val = engcalc.parse_value(comp.get("value"))
        if val is None or val <= 0:
            continue
        pf = val * 1e12
        total += pf
        who.append((ref, pf, None))
    return total, who


def _pin_base(func: str | None) -> str:
    """A pin's first name: PA14-BOOT0 is PA14, RDY0/SLRD is RDY0, and a
    bus name with a number tacked on is the bus name: SCL_24 is SCL
    (#1114)."""
    base = re.split(r"[-/]", _norm_pin(func))[0]
    return _strip_bus_number(base)


def _strip_bus_number(name: str) -> str:
    """SCL_24 -> SCL: a known bus signal with a _<number> suffix some
    symbol libraries add (#1114); any other name unchanged."""
    m = re.match(r"^(.*)_\d+$", name)
    if m and _BUS_ALIASES.get(m.group(1), m.group(1)) in _BUS_PARTNER:
        return m.group(1)
    return name


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


_PAIR_RE = re.compile(r"^(TIM\d+)_CH(\d)(N?)$")


def _pair_slots(hi: list[str], lo: list[str]) -> set[tuple[str, int]]:
    """{(TIMn, x)}: the timer channels whose CHx and CHxN outputs a
    high-side pin with functions HI and a low-side pin with LO bring out,
    either way round (CHxN's polarity is set in the timer)."""
    def sides(funcs):
        out: dict[tuple[str, int], set[str]] = {}
        for f in funcs:
            m = _PAIR_RE.match(f.upper())
            if m:
                out.setdefault((m.group(1), int(m.group(2))), set()).add(
                    m.group(3))
        return out
    h, lo_ = sides(hi), sides(lo)
    return {k for k in set(h) & set(lo_)
            if ("" in h[k] and "N" in lo_[k]) or ("N" in h[k] and ""
                                                   in lo_[k])}


def _pairs_one_timer(slots: list[set[tuple[str, int]]]) -> str | None:
    """A timer with a distinct channel pair for every leg, or None."""
    timers = (set.intersection(*({t for t, _ in s} for s in slots))
              if slots else set())
    for tim in sorted(timers, key=lambda t: int(t[3:])):
        opts = [{c for t, c in s if t == tim} for s in slots]
        match: dict[int, int] = {}

        def place(i, seen, opts=opts, match=match):
            for ch in opts[i]:
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


def _mcu_pwm_pairs(nets: dict, comps: dict,
                   comp_pins: dict[str, dict[str, str]], rails: dict,
                   table: dict) -> tuple[list[tuple], int]:
    """([(row, load_ref, mcu_ref, mcu_row, legs)], parts checked): each
    gate driver (a load row with pwm_pairs) whose high-side/low-side input
    pairs, all fed from one MCU, can't each take one channel's CHx and CHxN
    on a single timer. A leg is (hi_pin, lo_pin, hi_mcu_pin, lo_mcu_pin,
    hi_funcs, lo_funcs). A pair with an input that reaches no MCU pin (tied
    to a rail or ground, or driven by logic) is left out; a part with none
    left is not checked."""
    mcus = table.get("mcus") or []
    found: list[tuple] = []
    checked = 0

    def mcu_pin(net, ref):
        for reach in _zero_ohm_reach(net, nets, comps, comp_pins):
            if _supply_net(reach, nets.get(reach, []), rails):
                continue
            for m in nets.get(reach, []):
                mrow = (_mcu_row(comps.get(m["ref"], {}), mcus)
                        if m["ref"] != ref else None)
                if mrow and "timers" in mrow:
                    return m["ref"], mrow, _pin_base(m.get("pinfunction"))
        return None

    for row in table.get("loads") or []:
        if not row.get("pwm_pairs"):
            continue
        aliases = {_norm_pin(k): v.upper()
                   for k, v in (row.get("aliases") or {}).items()}
        for ref in sorted(comps):
            if not _spec_matches(row, comps[ref]):
                continue
            funcs = {aliases.get(_norm_pin(f), _norm_pin(f)): pn
                     for f, pn in _pin_funcs(ref, nets, comp_pins).items()}
            per_mcu: dict[str, list] = {}
            for hi, lo in row["pwm_pairs"]:
                hi, lo = hi.upper(), lo.upper()
                if hi not in funcs or lo not in funcs:
                    continue
                a = mcu_pin(funcs[hi][1], ref)
                b = mcu_pin(funcs[lo][1], ref)
                if not a or not b or a[0] != b[0]:
                    continue
                tims = a[1]["timers"] or {}
                per_mcu.setdefault(a[0], [a[1], []])[1].append(
                    (hi, lo, a[2], b[2], list(tims.get(a[2]) or []),
                     list(tims.get(b[2]) or [])))
            for mref, (mrow, legs) in sorted(per_mcu.items()):
                checked += 1
                slots = [_pair_slots(lg[4], lg[5]) for lg in legs]
                if _pairs_one_timer(slots) is None:
                    found.append((row, ref, mref, mrow, legs))
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
    i2c_addresses.yaml whose strap reads plainly, and every fixed-address
    device (a row with `address` and no `pin`) whose SCL is plainly a bus
    (another part's pin on it, no tie or pull to ground: a hub's
    SCL/CFG_SEL0 pulled down is a config strap) and whose `when` strap,
    if any, reads in one of its states."""
    buses: dict[str, list[tuple]] = {}
    for row in devices:
        for ref in sorted(comps):
            if not _spec_matches(row, comps[ref]):
                continue
            funcs: dict[str, tuple[str, str]] = {}
            for f, pn in _pin_funcs(ref, nets, comp_pins).items():
                funcs.setdefault(_pin_base(f), pn)
            scl = next((funcs[n.upper()] for n in row.get("scl") or ["SCL"]
                        if n.upper() in funcs), None)
            sda = next((funcs[n.upper()] for n in row.get("sda") or ["SDA"]
                        if n.upper() in funcs), None)
            if "pin" not in row:
                when = row.get("when") or {}
                at = next((funcs[n.upper()] for n in when.get("pins") or ()
                           if n.upper() in funcs), None)
                if when and (not at or not _net_states(
                        at[1], ref, at[0], nets, comps, comp_pins, rails,
                        {})[0] & set(when.get("states") or ())):
                    continue
                if scl and "address" in row and _i2c_bus_line(
                        scl, ref, nets, comps, comp_pins, rails):
                    buses.setdefault(scl[1], []).append(
                        (int(row["address"]), ref, row, "fixed"))
                continue
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


def _i2c_bus_line(scl: tuple[str, str], ref: str, nets: dict, comps: dict,
                  comp_pins: dict[str, dict[str, str]], rails: dict) -> bool:
    """True when REF's SCL pin (pin, net) sits on a bus: another part
    (not a resistor, capacitor or inductor) has a pin on the net, and
    nothing ties or pulls it to ground."""
    pin, net = scl
    if not any(m["ref"] != ref and _part_kind(m["ref"]) is None
               for m in nets.get(net, [])):
        return False
    st, _, _ = _net_states(net, ref, pin, nets, comps, comp_pins, rails, {})
    return not st & {"ground", "pulled_down", "open"}


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
    full = _norm_pin(m.get("pinfunction"))
    func = full.split("/", 1)[0]
    for spec in specs:
        if not _spec_matches(spec, comp):
            continue
        for row in spec.get("pins") or []:
            if row.get("function") and not re.search(row["function"], full,
                                                     re.I):
                continue
            rx = re.compile(row["pin"], re.I)
            if (func and rx.search(func)) or rx.search(str(m["pin"])):
                return spec, row
    return None


def _part_supply_v(ref: str, spec: dict, nets: dict,
                   comp_pins: dict[str, dict[str, str]],
                   rails: dict[str, float]) -> float | None:
    """The volts of a pin_leakage.yaml part's own supply, the rail its
    vil_frac / vih_frac and internal pull-up refer to (#1104): its pins
    whose function matches the spec's `supply` regex when it has one, else
    its power_in pins or pins named as a supply (VDD, VCC, VDDIO, ...);
    the highest known rail among them, None when none is known."""
    rx = re.compile(spec["supply"], re.I) if spec.get("supply") else None
    volts = []
    for pnum, net in comp_pins.get(ref, {}).items():
        if net not in rails or _is_ground_name(net):
            continue
        m = next((x for x in nets.get(net, []) if x["ref"] == ref
                  and x["pin"] == pnum), {})
        func = _norm_pin(m.get("pinfunction"))
        if rx is not None:
            if func and rx.search(func):
                volts.append(rails[net])
        elif (_base_type(m.get("pintype")) == "power_in"
              or _SUPPLY_PIN_RE.match(func)):
            volts.append(rails[net])
    return max(volts) if volts else None


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
    clean or when the pulls alone already put it in that band). Nets with a push-pull output on them are skipped: the driver
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
        # each listed pin's own supply (#1104): its thresholds and its
        # internal pull-up refer to that, not to the pull's rail
        vdd = {(ref, pin): _part_supply_v(ref, spec, nets, comp_pins, rails)
               for ref, pin, spec, _ in rows}
        # a listed internal pull-up (a reset pin's own) at its weakest
        for ref, pin, spec, row in rows:
            rail = vdd[(ref, pin)] or top
            if row.get("rpu") and rail > 0:
                pulls.append((rail, float(max(row["rpu"])),
                               f"{ref}.{pin} internal pull-up "
                               f"{max(row['rpu']) / 1e3:g}k max"))
        top = max(v for v, _, _ in pulls)
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
            rail = vdd[(ref, pin)] or top
            vil = row.get("vil", (row["vil_frac"] * rail
                                  if "vil_frac" in row else None))
            vih = row.get("vih", (row["vih_frac"] * rail
                                  if "vih_frac" in row else None))
            if vil is not None and vih is not None:
                recv.append((f"{ref}.{pin}", float(vil), float(vih)))
        if not recv:
            recv = [("CMOS", 0.3 * top, 0.7 * top)]
        def judge(v):
            if up:
                return ([r for r in recv if v < r[1]],
                        [r for r in recv if v < r[2]])
            return ([r for r in recv if v > r[2]],
                    [r for r in recv if v > r[1]])
        wrong, vague = judge(level)
        need = (max if up else min)(r[2 if up else 1] for r in recv)
        sev = "error" if wrong else "warning" if vague else None
        # a divider the designer set between the thresholds (an ADC or
        # analog bias input) is not the leakage's doing: judge only a band
        # the leakage moves the level into
        w0, v0 = judge(min(max(vth, 0.0), top))
        if sev == ("error" if w0 else "warning" if v0 else None):
            sev = None
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


def _verified_only(table):
    """A datasheet rule table with only its `verified: true` rows (#1100).

    A finding the rule makes from the whole table but not from this one
    rests on a row nobody has read against its datasheet (a part_rows.py
    draft), so the report lists it under not_judged, never as a fault."""
    if isinstance(table, dict):
        return {k: _verified_only(v) for k, v in table.items()}
    if isinstance(table, list):
        return [r for r in table
                if not isinstance(r, dict) or r.get("verified") is True]
    return table


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
# A battery charger has a current-limit pin too (BQ24074 ILIM, TMR) and no
# converter pin, but it brings BAT into regulation and its PGOOD says the
# input is good (#1223). A cell pin or a charge-set pin marks it: BQ24074
# (SLUS810) BAT, ISET, ITERM, TS, CHG; MCP73831 PROG, STAT; TP4056 PROG.
_CHARGER_PIN_RE = re.compile(
    r"^(V?BAT\d*|BATT|ISET\d*|ITERM|ICHG|PROG|TS|CHG|STAT\d*)$")


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
                and not any(_REGULATOR_PIN_RE.match(f) for f in fs)
                and not any(_CHARGER_PIN_RE.match(f) for f in fs)}
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


# power-path orientation (#1085). Seeded from TI SLVA139 and ABLIC S-8261
# Rev.5.5_00, not from any corpus board.
_PMOS_RE = re.compile(r"PMOS|P[-_ ]?CH|P-?FET|P-MOSFET|P-?CHANNEL|Q_P(MOS)?_",
                      re.I)
_NMOS_RE = re.compile(r"NMOS|N[-_ ]?CH|N-?FET|N-MOSFET|N-?CHANNEL|Q_N(MOS)?_",
                      re.I)
# MOSFETs whose part number names no channel, from their datasheets'
# titles: exact parts (an A / E / L suffix or a reel code may follow)
# and the families whose naming carries it (Diodes Inc DMN / DMP,
# Toshiba SSM3K / SSM6K N and SSM3J / SSM6J P, IR's IRF9 P). GaN parts
# (EPC) have no body diode and stay unknown.
_FET_MPN_CHANNEL = (
    (re.compile(r"^(2N700[02]|BSS138|BSS123|AO340[024]|AO3416|AO4406"
                r"|AON7264|CJ2310|NTMFS5C628N|DMN\d|SSM[36]K\d)"), "N"),
    (re.compile(r"^(BSS84|AO340[179]|AO3415|AO440[79]|AO4435|AO4485"
                r"|DMP\d|SSM[36]J\d|IRF9)"), "P"),
)
# a single-cell battery's positive net: BAT, VBAT, BATT, +BATT, BAT+,
# VBAT_IN; not BAT- / BATN / BAT_NEG
_BATTERY_NET_RE = re.compile(
    r"^\+?V?BATT?(\+|_?P|_?POS|_IN)?$|^\+?CELL\+?$|^VCELL$")
# a rail that has a source of its own besides the battery: a bus or adapter
# input by name
_SOURCE_NET_RE = re.compile(r"VBUS|^\+?VIN|USB|^\+?5V|^\+?VDC|ADAPTER|"
                            r"^\+?DC_?IN|CHG|CHARGE")
_BATTERY_REF_RE = re.compile(r"^BT\d")
# S-8261-style protector: VDD to the cell's +, VSS to the cell's -, VM
# (through R2) to the pack's - (EB-), DO/CO drive the two FETs
_PROTECTOR_PINS = {"VDD", "VSS", "VM", "DO", "CO"}


def _fet_channel(comp: dict) -> str | None:
    """'P' or 'N' from the part's value, symbol name or the symbol's
    description ("P-Channel MOSFET"), or a part number whose datasheet
    names it (_FET_MPN_CHANNEL), else None."""
    for text in (comp.get("value") or "",
                 (comp.get("libsource") or ("", ""))[1],
                 comp.get("libdesc") or ""):
        p, n = _PMOS_RE.search(text), _NMOS_RE.search(text)
        if p and not n:
            return "P"
        if n and not p:
            return "N"
    for text in (comp.get("value") or "",
                 (comp.get("libsource") or ("", ""))[1]):
        for rx, ch in _FET_MPN_CHANNEL:
            if rx.match(text.strip().upper()):
                return ch
    return None


def _battery_nets(nets: dict) -> set[str]:
    """Nets that are a battery's positive terminal: by name, or on a BT*
    part's + pin (or pin 1 of an unnamed one)."""
    out = set()
    for name, members in nets.items():
        bare = re.split(r"/", name.upper())[-1]
        if _BATTERY_NET_RE.match(bare):
            out.add(name)
            continue
        for m in members:
            if not _BATTERY_REF_RE.match(m["ref"] or ""):
                continue
            func = _norm_pin(m.get("pinfunction"))
            if func == "+" or (not func and m["pin"] == "1"):
                out.add(name)
    return {n for n in out if not _is_ground_name(n.lstrip("/"))}


def _sourced_rail(net: str, nets: dict, comp_pins: dict[str, dict[str, str]],
                  skip: str, _hop: int = 0) -> bool:
    """A rail with a source besides the battery: a power_out pin of
    another part, a connector pin, or a bus/adapter name, on the net or
    one or two diode (anode side), fuse, inductor, bead or resistor hops
    back."""
    bare = re.split(r"/", net.upper())[-1]
    if _SOURCE_NET_RE.search(bare):
        return True
    members = nets.get(net, [])
    if any(m["ref"] != skip and (
            _base_type(m.get("pintype")) == "power_out"
            or _CONNECTOR_RE.match(m["ref"] or "")) for m in members):
        return True
    if _hop >= 2:
        return False
    for m in members:
        xp = comp_pins.get(m["ref"], {})
        if m["ref"] == skip or len(xp) != 2:
            continue
        func = _norm_pin(m.get("pinfunction"))
        diode = (m["ref"] or "").startswith("D")
        if diode and func not in ("K", "CATHODE"):
            continue
        if not diode and not _DC_PASS_RE.match(m["ref"] or ""):
            continue
        far = next((n for p, n in xp.items() if p != m["pin"]), None)
        if far and far != net and not _is_ground_name(far.lstrip("/")) \
                and _sourced_rail(far, nets, comp_pins, skip, _hop + 1):
            return True
    return False


def _power_path_fets(nets: dict, comps: dict,
                     comp_pins: dict[str, dict[str, str]], rails: dict):
    """Yield (ref, channel, battery net, other net) for each MOSFET whose
    body diode conducts from a sourced rail into a battery's positive net
    (N: source to drain, P: drain to source). The last item is
    ('checked', n, '', '')."""
    batteries = _battery_nets(nets)
    checked = 0
    for ref, pins in sorted(comp_pins.items()):
        if not ref.startswith("Q") or comps.get(ref, {}).get("dnp"):
            continue
        roles: dict[str, set[str]] = {}
        for net, members in nets.items():
            for m in members:
                if m["ref"] == ref:
                    r = _transistor_role(_norm_pin(m.get("pinfunction")))
                    if r:
                        roles.setdefault(r, set()).add(net)
        if len(roles.get("D", ())) != 1 or len(roles.get("S", ())) != 1:
            continue
        ch = _fet_channel(comps.get(ref, {}))
        if not ch:
            continue
        (d,), (s,) = roles["D"], roles["S"]
        anode, cathode = (s, d) if ch == "N" else (d, s)
        if not ({anode, cathode} & batteries):
            continue
        checked += 1
        if (cathode in batteries and anode not in batteries
                and not _is_ground_name(anode.lstrip("/"))
                and _supply_net(anode, nets[anode], rails)
                and _sourced_rail(anode, nets, comp_pins, ref)):
            yield ref, ch, cathode, anode
    yield "checked", checked, "", ""


# a controller pin that drives a FET gate (ideal-diode, hot-swap, load-
# switch, eFuse and gate-driver ICs face their FETs either way round)
_GATE_CTRL_RE = re.compile(r"GATE|^G\d*$|^GD|^[HL]G\d*$|^[HL]O\d*$|DRV|"
                           r"^DG|^CG|^OUT[HL]?$")
_NEG_NET_RE = re.compile(r"^-|NEG|VEE|^VSS|^V-")


def _gate_drive(gate: str, nets: dict, comp_pins: dict[str, dict[str, str]],
                own: str, rails: dict) -> str:
    """How a FET's gate net is held, on the net or one resistor hop away
    (a resistor to a rail or ground is a pull, not a drive): 'controller'
    (an IC's gate-drive pin), 'driven' (another transistor's drain or
    collector, or an IC GPIO pin), 'tied' (ground, directly or through a
    resistor, with no IC or transistor on it), else 'other'."""
    import signal_domains as sd  # noqa: PLC0415
    kinds: set[str] = set()
    seen = [gate]
    if _is_ground_name(gate.lstrip("/")):
        return "tied"
    for m in nets.get(gate, []):
        xp = comp_pins.get(m["ref"], {})
        if m["ref"] == own or _part_kind(m["ref"]) != "R" or len(xp) != 2:
            continue
        far = next((n for p, n in xp.items() if p != m["pin"]), None)
        if not far or far in seen:
            continue
        if _is_ground_name(far.lstrip("/")):
            kinds.add("ground")
        elif not _supply_net(far, nets.get(far, []), rails):
            seen.append(far)
    for net in seen:
        for m in nets.get(net, []):
            ref = m["ref"] or ""
            if ref == own:
                continue
            func = _norm_pin(m.get("pinfunction"))
            role = _transistor_role(func)
            if ref.startswith("Q"):
                kinds.add("driven" if role in ("D", "C") else "other")
            elif ref.startswith(("U", "IC")):
                if _GATE_CTRL_RE.search(func):
                    kinds.add("controller")
                elif sd._GPIO.match(func):
                    kinds.add("driven")
                else:
                    kinds.add("other")
    if "controller" in kinds:
        return "controller"
    if "other" in kinds:
        return "other"
    if "driven" in kinds:
        return "driven"
    return "tied" if kinds == {"ground"} else "other"


def _power_switches(nets: dict, comps: dict,
                    comp_pins: dict[str, dict[str, str]], rails: dict):
    """Yield (ref, channel, case, supply net, other net) for each MOSFET
    switch off the battery nets whose body diode conducts the way it
    should block (N: source to drain, P: drain to source):
    'high' a gate-driven high-side switch with the diode from its sourced
    rail into the load; 'low' a gate-driven N-FET with its drain on ground
    and its source on the load; 'reverse' a P-FET with its gate tied to
    ground and its source, not its drain, on the input (no reverse
    protection). The last item is ('checked', n, not_judged counts, '',
    '')."""
    batteries = _battery_nets(nets)
    checked, nj = 0, {}

    def skip(why):
        nj[why] = nj.get(why, 0) + 1

    for ref, pins in sorted(comp_pins.items()):
        if not ref.startswith("Q") or comps.get(ref, {}).get("dnp"):
            continue
        roles: dict[str, set[str]] = {}
        for net, members in nets.items():
            for m in members:
                if m["ref"] == ref:
                    r = _transistor_role(_norm_pin(m.get("pinfunction")))
                    if r:
                        roles.setdefault(r, set()).add(net)
        if any(len(roles.get(k, ())) != 1 for k in "GDS"):
            continue
        (g,), (d,), (s,) = roles["G"], roles["D"], roles["S"]
        if {d, s} & batteries:
            continue            # rule 22's
        ch = _fet_channel(comps.get(ref, {}))
        if not ch:
            skip("channel not stated")
            continue
        if d == s or g in (d, s):
            continue
        drive = _gate_drive(g, nets, comp_pins, ref, rails)
        if drive == "controller":
            skip("gate from a controller")
            continue
        if drive not in ("driven", "tied"):
            skip("gate tied to a rail or not traced")
            continue
        gnd = {n for n in (d, s) if _is_ground_name(n.lstrip("/"))}
        if any(_NEG_NET_RE.search(re.split(r"/", n.upper())[-1])
               for n in (d, s) if n not in gnd):
            skip("negative rail")
            continue
        if gnd:
            if drive == "tied" or ch == "P" or len(gnd) == 2:
                skip("tied gate or P-FET on the low side")
                continue
            checked += 1
            if d in gnd:
                yield ref, ch, "low", d, s
            continue
        if not any(_supply_net(n, nets[n], rails) for n in (d, s)):
            skip("no supply on either side")
            continue
        src = {n for n in (d, s) if _sourced_rail(n, nets, comp_pins, ref)}
        if len(src) != 1:
            skip("both sides sourced or neither")
            continue
        (sup,) = src
        load = s if sup == d else d
        if drive == "tied":
            if ch != "P":
                skip("tied gate or P-FET on the low side")
                continue
            checked += 1
            if sup == s:
                yield ref, ch, "reverse", sup, load
            continue
        checked += 1
        if (ch == "P" and sup == d) or (ch == "N" and sup == s):
            yield ref, ch, "high", sup, load
    yield "checked", checked, nj, "", ""


def _protector_orientation(nets: dict, comps: dict,
                           comp_pins: dict[str, dict[str, str]]):
    """Yield (ref, vss net, vm-side net, ics on vss, ics on vm side) for
    each S-8261-style protector whose VSS (the cell's -) net carries more
    other ICs' ground pins than the pack - net VM reaches through its one
    resistor: the load returns to the cell's - around the FETs. The last
    item is ('checked', n, '', [], [])."""
    funcs: dict[str, dict[str, str]] = {}
    for net, members in nets.items():
        for m in members:
            funcs.setdefault(m["ref"], {})[_norm_pin(
                m.get("pinfunction"))] = net
    checked = 0

    def grounds(net: str, own: str) -> list[str]:
        return sorted({m["ref"] for m in nets.get(net, [])
                       if m["ref"] != own and (m["ref"] or "").startswith("U")
                       and _base_type(m.get("pintype")) in _POWER_TYPES})

    for ref, fn in sorted(funcs.items()):
        if not _PROTECTOR_PINS <= set(fn):
            continue
        vss, vm = fn["VSS"], fn["VM"]
        pack = None
        for m in nets.get(vm, []):
            xp = comp_pins.get(m["ref"], {})
            if _part_kind(m["ref"]) == "R" and len(xp) == 2:
                pack = next((n for p, n in xp.items() if p != m["pin"]), None)
        if not pack or pack == vss:
            continue
        checked += 1
        on_vss, on_pack = grounds(vss, ref), grounds(pack, ref)
        if on_vss and len(on_vss) > len(on_pack):
            yield ref, vss, pack, on_vss, on_pack
    yield "checked", checked, "", [], []


# Class 6 of docs/miss-taxonomy.md: a pin whose role doesn't fit its net.
# A pin's role is its symbol's pin name (the stock symbol's, when the
# part is placed from KiCad's library): a ground pin, a supply pin, a
# battery-backup pin.
_ROLE_SKIP_RE = re.compile(r"^(J|P|CN|X|TP|JP|H|MH|SB|NT|SW|K)\d")
_ROLE_SUPPLY_RE = re.compile(
    r"^(VCC|VDD|VBUS|VIN|AVDD|DVDD|VDDA|VDDIO|IOVDD|VCCIO|VCCA|VCCB|VS)"
    r"\d*$")
_VBAT_PIN_RE = re.compile(r"^VBAT\d*$")
_POS_NET_RE = re.compile(
    r"^\+|^(VCC|VDD|VBUS|VIN|VSYS|VBAT|VBATT|V_?USB)(_|$)")


def _pin_role(func: str | None) -> str | None:
    """'ground', 'supply', 'vbat' or None, from a symbol's pin name."""
    name = re.sub(r"[_{}]", "", _pin_base(func)) if func else ""
    if not name:
        return None
    if _GROUND_RE.search(name):
        return "ground"
    if _VBAT_PIN_RE.match(name):
        return "vbat"
    if _ROLE_SUPPLY_RE.match(name):
        return "supply"
    return None


def _net_polarity(net: str, rails: dict) -> str | None:
    """'ground' or 'positive' when the net's name or volts say so."""
    if net.startswith("unconnected-"):
        return None
    if _is_ground_name(net) or rails.get(net) == 0:
        return "ground"
    if (rails.get(net) or 0) > 0 or _POS_NET_RE.search(
            net.lstrip("/").upper()):
        return "positive"
    return None


def _pin_role_nets(nets: dict, comps: dict, rails: dict):
    """Yield (ref, [(sev, pin, func, role, net)]) for each part with a
    ground pin on a positive rail (error), a supply pin on ground
    (warning) or a VBAT pin on ground (info: an RTC's backup pin is
    grounded when unused). The last item is ('checked', n)."""
    hits: dict[str, list] = {}
    seen: set[str] = set()
    for net, members in sorted(nets.items()):
        pol = _net_polarity(net, rails)
        for m in members:
            ref = m["ref"]
            if (_ROLE_SKIP_RE.match(ref or "")
                    or (comps.get(ref) or {}).get("dnp")):
                continue
            role = _pin_role(m.get("pinfunction"))
            if role is None:
                continue
            seen.add(ref)
            sev = {("ground", "positive"): "error",
                   ("supply", "ground"): "warning",
                   ("vbat", "ground"): "info"}.get((role, pol))
            if sev:
                hits.setdefault(ref, []).append(
                    (sev, m["pin"], m.get("pinfunction"), role, net))
    for ref in sorted(hits):
        yield ref, hits[ref]
    yield "checked", len(seen)


# A battery protector's pins: its cell side (GND/VSS on the cell's -) and
# its load side (BAT-/VM/P-/EB-, toward the pack's - the load returns to).
_PROT_CELL_PINS = {"GND", "VSS"}
_PROT_LOAD_PINS = {"BAT-", "VM", "P-", "EB-", "PACK-"}
_CELL_REF_RE = re.compile(r"^(BAT|BT)\d")


def _battery_net(net: str, nets: dict, comps: dict) -> bool:
    """NET is the cell's own terminal: a battery part sits on it, or its
    name says BAT."""
    if "BAT" in net.upper():
        return True
    for m in nets.get(net, []):
        comp = comps.get(m["ref"]) or {}
        lib = " ".join((comp.get("libsource") or ("", ""))).upper()
        if _CELL_REF_RE.match(m["ref"] or "") or "BATTERY" in lib:
            return True
    return False


def _protector_cell_side(nets: dict, comps: dict):
    """Yield (ref, cell pin, cell net, load pin, load net) for each
    protector whose cell-side pin is on the board's ground while its
    load-side pin sits on the cell's own terminal: the two swapped, so the
    FETs sit in no path and the protector cuts nothing. The last item is
    ('checked', n, '', '', '')."""
    funcs: dict[str, dict[str, tuple[str, str]]] = {}
    for net, members in nets.items():
        for m in members:
            funcs.setdefault(m["ref"], {})[_norm_pin(
                m.get("pinfunction"))] = (m["pin"], net)
    checked = 0
    for ref, fn in sorted(funcs.items()):
        cell = sorted(_PROT_CELL_PINS & set(fn))
        load = sorted(_PROT_LOAD_PINS & set(fn))
        if not cell or not load or (comps.get(ref) or {}).get("dnp"):
            continue
        checked += 1
        cnet, lnet = fn[cell[0]][1], fn[load[0]][1]
        if (cnet != lnet and _is_ground_name(cnet)
                and not _is_ground_name(lnet)
                and _battery_net(lnet, nets, comps)):
            yield ref, cell[0], cnet, load[0], lnet
    yield "checked", checked, "", "", ""


# An MCU's serial-wire debug pins (mcu_pins.yaml debug)
_DEBUG_NET_RE = {"SWDIO": re.compile(r"SWDIO|SWDAT|SWD_IO|SWD_DIO"),
                 "SWCLK": re.compile(r"SWCLK|SWCK|SWD_CLK")}
_DEBUG_PORT_RE = re.compile(r"^(J|P|CN|X|TP)\d")


def _debug_signal(net: str, nets: dict) -> str | None:
    """The SWD signal NET carries by its name or a connector pin's name."""
    names = [net.upper()] + [_norm_pin(m.get("pinfunction"))
                             for m in nets.get(net, [])
                             if _DEBUG_PORT_RE.match(m["ref"] or "")]
    for sig, rx in _DEBUG_NET_RE.items():
        if any(rx.search(n) for n in names):
            return sig
    return None


def _pin_tokens(func: str | None) -> set[str]:
    """Every name a pin goes by: PA13(SWDIO) is PA13 and SWDIO,
    PA15/PA14-BOOT0 is PA15, PA14 and BOOT0, SYS_SWCLK is SWCLK too."""
    return {t for t in re.split(r"[-/()_ ,]+", _norm_pin(func)) if t}


def _debug_reach(net: str, nets: dict, comps: dict,
                 comp_pins: dict[str, dict[str, str]]) -> list[str]:
    """NET and the nets one 2-pin resistor away (a series resistor to the
    debug header)."""
    out = [net]
    for m in nets.get(net, []):
        pins = comp_pins.get(m["ref"], {})
        if _part_kind(m["ref"]) == "R" and len(pins) == 2:
            far = next((n for p, n in pins.items() if p != m["pin"]), None)
            if far and far not in out and not far.startswith("unconn"):
                out.append(far)
    return out


def _debug_pins(nets: dict, comps: dict,
                comp_pins: dict[str, dict[str, str]], table: dict):
    """Yield ('wrong', row, mcu, sig, pin, func, net) for an SWD line that
    reaches the MCU on a pin other than the one its debug port has, and
    ('reused', row, mcu, sig, pin, func, net) for an SWD pin wired to
    other parts with no debug header, test point or SWD-named net within
    one resistor: debugging is locked out once firmware takes the pin.
    The last item is ('checked', n, ...)."""
    rows = [r for r in (table.get("mcus") or []) if r.get("debug")]
    checked = 0
    for ref in sorted(comps):
        row = _mcu_row(comps[ref], rows) if rows else None
        if not row or comps[ref].get("dnp"):
            continue
        checked += 1
        want = {s.upper(): p.upper() for s, p in row["debug"].items()}
        pins = _pin_funcs(ref, nets, comp_pins)
        by_name = {t: (pn, net, f) for f, (pn, net) in pins.items()
                   for t in _pin_tokens(f)}
        for f, (pn, net) in sorted(pins.items()):
            sig = _debug_signal(net, nets)
            if sig in want and not {want[sig], sig} & _pin_tokens(f):
                yield "wrong", row, ref, sig, pn, f, net
        for sig, mpin in sorted(want.items()):
            at = by_name.get(mpin) or by_name.get(sig)
            if not at or at[1].startswith("unconnected-"):
                continue
            reach = _debug_reach(at[1], nets, comps, comp_pins)
            others = [m for n in reach for m in nets.get(n, [])
                      if m["ref"] != ref]
            if not others or any(
                    _debug_signal(n, nets) for n in reach) or any(
                    _DEBUG_PORT_RE.match(m["ref"] or "") for m in others):
                continue
            yield "reused", row, ref, sig, at[0], at[2], at[1]
    yield "checked", checked, None, None, None, None, None


# Design intent (class 8 of docs/miss-taxonomy.md, #1137).
#
# A named bus whose members a master has to read together: a part that
# takes all of them but one, while that one still reaches the connector the
# others come in on, has dropped it. Zilog UM0080 (Z80 CPU User Manual),
# pin descriptions: MREQ "indicates that the address bus holds a valid
# address for a memory read or memory write operation", IORQ "indicates
# that the lower half of the address bus holds a valid I/O address for an
# I/O read or write operation", RD and WR say which way; a part decoding
# bus cycles from RD and WR needs both MREQ and IORQ to tell a memory cycle
# from an I/O one. IEEE 1149.1 section 3: TCK, TMS, TDI and TDO are the
# four required TAP signals. A byte-wide data port reads D0..D7. SPI is left
# out: a write-only device has no MISO by design.
_BUS_GROUPS = (
    ("Z80 bus-cycle strobes", ("MREQ", "IORQ", "RD", "WR"),
     "Zilog UM0080 pin descriptions: MREQ and IORQ tell a memory cycle "
     "from an I/O one, RD and WR its direction"),
    ("JTAG TAP", ("TCK", "TMS", "TDI", "TDO"),
     "IEEE 1149.1 section 3: TCK, TMS, TDI and TDO are the required TAP "
     "signals"),
    ("data byte", tuple(f"D{i}" for i in range(8)),
     "a byte-wide data port reads all of D0..D7"),
)
_BUS_MASTER_MIN_PINS = 20
# a buffer, latch or gate passes the bus on; it doesn't read it
_LOGIC_VALUE_RE = re.compile(r"74[A-Z]*\d{2,}|^CD4\d{3}|^4\d{3}[A-Z]?$",
                             re.I)


def _bus_name(name: str) -> str:
    """A net or pin name with its path, ~{} and active-low marks gone."""
    name = _norm_pin(name.rsplit("/", 1)[-1])
    return re.sub(r"[#*{}~/ ]", "", name)


def _bus_member(name: str, member: str) -> str | None:
    """The prefix NAME carries before bus member MEMBER (ZXMREQ: 'ZX',
    LCD_D3: 'LCD_'), or None when NAME isn't that member."""
    m = re.fullmatch(rf"((?:[A-Z0-9]*?_)?(?:ZX|Z80_?|CPU_?)?)N?{member}"
                     r"(?:_N|_B|N)?", _bus_name(name))
    return m.group(1) if m else None


def _bus_members_dropped(nets: dict, comps: dict,
                         comp_pins: dict[str, dict[str, str]]):
    """Yield (group, member, net, master, read, connectors, source) for a
    bus member that reaches a connector the rest of its group comes in on,
    while a part of 20 pins or more reads every other member (directly or
    through one series resistor) but not this one. The last item is
    ('checked', n, ...)."""
    checked = 0
    for gname, members, src in _BUS_GROUPS:
        # (prefix) -> member -> net
        found: dict[str, dict[str, str]] = {}
        for net, ms in nets.items():
            if net.startswith("unconnected-"):
                continue
            for mem in members:
                pre = _bus_member(net, mem)
                if pre is None:
                    continue
                found.setdefault(pre, {}).setdefault(mem, net)
        for pre, have in sorted(found.items()):
            if len(have) < len(members):
                continue
            checked += 1
            reach = {mem: _debug_reach(net, nets, comps, comp_pins)
                     for mem, net in have.items()}
            conns = {mem: {m["ref"] for n in r for m in nets.get(n, [])
                           if _OB_CONN_RE.match(m["ref"] or "")}
                     for mem, r in reach.items()}
            parts = {mem: {m["ref"] for n in r for m in nets.get(n, [])
                           if (m["ref"] or "").startswith("U")}
                     for mem, r in reach.items()}
            for ref in sorted(set().union(*parts.values())):
                if len(comp_pins.get(ref, {})) < _BUS_MASTER_MIN_PINS \
                        or comps.get(ref, {}).get("dnp") \
                        or _LOGIC_VALUE_RE.search(
                            comps.get(ref, {}).get("value") or ""):
                    continue
                miss = [mem for mem in members if ref not in parts[mem]]
                if len(miss) != 1:
                    continue
                mem = miss[0]
                # another chip on the board reads it (pico-if1 v1.3: a
                # 4075 takes MREQ for the ROM, the I/O Pico needs only IORQ)
                if parts[mem]:
                    continue
                read = [m for m in members if m != mem]
                shared = conns[mem] & set().union(
                    *(conns[m] for m in read))
                if not shared:
                    continue
                yield (gname, mem, have[mem], ref, read, sorted(shared),
                       src)
    yield "checked", checked, None, None, None, None, None


# A UART line whose name says which way it runs (RX_FROM_AUX,
# TX_TO_CONTROL_BOARD) reaching an MCU pin whose fixed UART function is the
# other way (errata corpus: smart-lcc rev A put RX_FROM_AUX on the Nano
# RP2040 Connect's D8, GPIO20, which is UART1 TX).
_UART_NET_DIR = (("RX", re.compile(r"(^|[/_])RXD?_FROM(_|$)")),
                 ("TX", re.compile(r"(^|[/_])TXD?_TO(_|$)")))


def _uart_dir_row(ref: str, comp: dict, rows: list[dict],
                  stated: dict[str, str]) -> dict | None:
    """REF's mcu_pins.yaml row: what design_intent.modules says it is,
    else its Value."""
    if ref in stated:
        return _mcu_row({"value": stated[ref]}, rows)
    return _mcu_row(comp, rows)


def _uart_directions(nets: dict, comps: dict,
                     comp_pins: dict[str, dict[str, str]], table: dict,
                     stated: dict[str, str]):
    """Yield (ref, pin, func, function, net, way, row) for an MCU pin on a
    net whose name gives its direction (`way`, from this board's side)
    when the pin's UART function, by its own name (TX, RXD) or the row's
    module_pins and the chip's uart table, is the opposite. A series
    resistor in between is followed. The last item is ('checked', n, ...)."""
    rows = table.get("mcus") or []
    chips = {r["mpn"]: r for r in rows if r.get("uart")}
    checked = 0
    for ref in sorted(comps):
        if comps[ref].get("dnp") or not ref.startswith(("U", "A", "M")):
            continue
        row = _uart_dir_row(ref, comps[ref], rows, stated)
        chip = row if row and row.get("uart") else None
        if row and row.get("module_of"):
            chip = next((c for m, c in chips.items()
                         if m.startswith(row["module_of"])), None)
        mod = (row or {}).get("module_pins") or {}
        seen = False
        for func, (pn, net) in sorted(_pin_funcs(ref, nets,
                                                 comp_pins).items()):
            toks = _pin_tokens(func)
            fn = None
            if toks & {"TX", "TXD", "TXO"}:
                fn = "TX"
            elif toks & {"RX", "RXD", "RXI"}:
                fn = "RX"
            elif chip:
                for t in toks:
                    g = str(mod.get(t, t))
                    u = (chip.get("uart") or {}).get(g)
                    if u:
                        fn = f"{g} {u}"
                        break
            if not fn:
                continue
            seen = True
            for n in _debug_reach(net, nets, comps, comp_pins):
                way = next((w for w, rx in _UART_NET_DIR
                            if rx.search(n.upper())), None)
                if way and not fn.endswith(way):
                    yield ref, pn, func, fn, n, way, row
                    break
        checked += seen
    yield "checked", checked, None, None, None, None, None


# A bus transceiver ('245 family) enabled all the time, with one side on a
# host connector's lines and DIR straight off a connector strobe: it drives
# the host's bus on every cycle of that strobe, whoever is addressed
# (errata corpus: pico-if1 v1.0 tied the 74LS245's OE to ground and put DIR
# on the edge connector's raw /RD; v1.1 drives DIR from a decoded ROM read).
# TI SN74LS245 (SDLS146) function table: OE low enables the outputs, DIR
# low sends B to A and DIR high A to B, so whichever side faces the host is
# driven for one level of DIR.
_XCVR_RE = re.compile(r"74[A-Z]*(245|640|645)|74[A-Z]*8T245|LVC[H]?4245",
                      re.I)
_XCVR_DEFAULT = {"DIR": "1", "OE": "19"}


def _host_bus_transceivers(nets: dict, comps: dict,
                           comp_pins: dict[str, dict[str, str]],
                           rails: dict):
    """Yield (ref, oe_pin, oe_net, dir_pin, dir_net, side_nets, conns) for
    each always-on '245 whose DIR comes straight from a connector while
    four or more of one side's data pins sit on that connector's nets. The
    last item is ('checked', n, ...)."""
    checked = 0
    for ref in sorted(comps):
        comp = comps[ref]
        if comp.get("dnp") or not _XCVR_RE.search(
                f"{comp.get('value') or ''} "
                f"{(comp.get('libsource') or ('', ''))[1]}"):
            continue
        funcs = _pin_funcs(ref, nets, comp_pins)
        by = {}
        for f, (pn, net) in funcs.items():
            toks = _pin_tokens(f)
            if "DIR" in toks or "T/R" in _norm_pin(f) or "->" in f:
                by["DIR"] = (pn, net)
            elif toks & {"OE", "G", "CE", "E"} or _norm_pin(f) in {
                    "~OE", "OE#", "~G", "~CE"}:
                by["OE"] = (pn, net)
        pins = comp_pins.get(ref, {})
        for k, pn in _XCVR_DEFAULT.items():
            if k not in by and pn in pins and len(pins) == 20:
                by[k] = (pn, pins[pn])
        if set(by) != {"DIR", "OE"}:
            continue
        checked += 1
        (oe_pin, oe_net), (dir_pin, dir_net) = by["OE"], by["DIR"]
        if _net_polarity(oe_net, rails) != "ground":
            continue
        # DIR tied to a rail is a one-way buffer, not a strobe-driven one
        if _net_polarity(dir_net, rails) is not None:
            continue
        dconn = {m["ref"] for m in nets.get(dir_net, [])
                 if _OB_CONN_RE.match(m["ref"] or "")}
        drivers = [m for m in nets.get(dir_net, [])
                   if m["ref"] != ref and not _OB_CONN_RE.match(
                       m["ref"] or "") and _part_kind(m["ref"]) is None
                   and _base_type(m.get("pintype")) in _OB_DRIVE_TYPES
                   # another transceiver's data pin passes the strobe on
                   and not _XCVR_RE.search(
                       comps.get(m["ref"], {}).get("value") or "")]
        if not dconn or drivers:
            continue
        side = [net for f, (pn, net) in funcs.items()
                if re.fullmatch(r"[AB]\d", _norm_pin(f) or "")
                and any(m["ref"] in dconn for m in nets.get(net, []))]
        if len(side) < 4:
            continue
        yield ref, oe_pin, oe_net, dir_pin, dir_net, sorted(side), \
            sorted(dconn)
    yield "checked", checked, None, None, None, None, None


# An off-board input: a connector pin whose net is held only by a resistor
# to a rail, read by an IC input or a transistor's base or gate. TI SPRACK9
# (Appendix A Table 2) on a cable line that "is pulled up to eliminate
# switching if the XDS is disconnected": "TI recommends a 4.7K pull-up but
# common values in the 4.7K to 10K range are acceptable." Microchip AN2587
# (DS00002587A, PCB rule 8): "The use of TVS and ferrite beads and/or common
# mode chokes as required are recommended on all external connector I/O
# pins." An RC filter behind a series resistor is not enough on its own:
# pdlc v1 had 20k into 100n at its PNP's base and the errata still calls
# the EN input "much too sensitive when left floating".
_OFFBOARD_PULL_MAX = 10e3
_CLAMP_VALUE_RE = re.compile(
    r"(ESD|TVS|PESD|SP0\d|TPD\d|USBLC|SMAJ|SMBJ|SMCJ|SMF\d|PRTR|RCLAMP|"
    r"SRV05|IP4\d|NUP\d|ESDA|CDSOT|TPSMA|P6KE|1\.5KE)", re.I)
_OB_DRIVE_TYPES = {"output", "tri_state", "power_out", "open_collector",
                "open_emitter"}
# a pin read as an analogue level (a converter's feedback, a sense input)
# is not a logic input, and a net its name calls an output (TX, ..._OUT) is
# driven from this board whatever its pin types say
_ANALOG_PIN_RE = re.compile(r"^(V?FB\d*|COMP|V?SENSE\w*|I?SNS\w*|CS[+-]?|"
                            r"ISET|ILIM|RT|SS|TR|AIN\d*|ADC\w*|VREF\w*)$")
_OUTPUT_TOKEN_RE = re.compile(r"^(TXD?|TXO|OUT\w*|\w*OUT)$")


def _offboard_weak_pull(nets: dict, comps: dict,
                        comp_pins: dict[str, dict[str, str]],
                        rails: dict[str, float]):
    """Yield one dict per connector net held only by a pull resistor weaker
    than _OFFBOARD_PULL_MAX to a rail or ground, with no on-board driver and
    no clamp (TVS or ESD diode) on it, read by an IC input or a transistor's
    base or gate on the net or one 2-pin resistor on. The last item is
    {'checked': n}, the off-board inputs looked at."""
    checked = 0
    for net, members in sorted(nets.items()):
        if (net.startswith("unconnected-") or _is_ground_name(net)
                or _supply_net(net, members, rails)):
            continue
        conns = [m for m in members if _CONNECTOR_RE.match(m["ref"] or "")]
        if not conns or any(_OUTPUT_TOKEN_RE.match(t) for t in
                            re.split(r"[^A-Z0-9]+", net.upper()) if t):
            continue
        pulls, series, clamps, drivers = [], [], [], []
        for m in members:
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            kind = _part_kind(ref)
            if (_base_type(m.get("pintype")) in _OB_DRIVE_TYPES
                    or (ref.startswith("Q") and _transistor_role(
                        m.get("pinfunction")) in ("C", "D", "E", "S"))):
                drivers.append(f"{ref}.{m['pin']}")
            if kind == "R" and len(pins) == 2:
                far = next((n for p, n in pins.items() if p != m["pin"]),
                           None)
                ohm = engcalc.parse_value(comps.get(ref, {}).get("value"))
                if not far or not ohm:
                    continue
                fv = 0.0 if _is_ground_name(far) else rails.get(far)
                if fv is not None:
                    pulls.append((ohm, fv, ref, far))
                else:
                    series.append((ref, far))
            elif (ref.startswith(("D", "TVS", "ESD"))
                  and not ref.startswith("DS")) or (
                    ref.startswith("U") and _CLAMP_VALUE_RE.search(
                        str(comps.get(ref, {}).get("value") or ""))):
                clamps.append(ref)
        if not pulls or drivers:
            continue
        # readers: on the net, or behind one series resistor
        readers = []
        for where, via in [(net, None)] + [(far, r) for r, far in series]:
            for y in nets.get(where, []):
                yref = y["ref"] or ""
                role = _transistor_role(y.get("pinfunction"))
                if yref.startswith("Q") and role in ("B", "G"):
                    readers.append((f"{yref}.{role}", via))
                elif (yref.startswith("U") and _base_type(y.get("pintype"))
                      in ("input", "bidirectional")
                      and not _ANALOG_PIN_RE.match(
                          _norm_pin(y.get("pinfunction")))):
                    readers.append((f"{yref}.{_norm_pin(y.get('pinfunction')) or y['pin']}", via))
                elif via and _base_type(y.get("pintype")) in _OB_DRIVE_TYPES:
                    readers = []
                    break
            else:
                continue
            break
        if not readers:
            continue
        checked += 1
        ohm, fv, pref, far = min(pulls)
        if ohm <= _OFFBOARD_PULL_MAX or clamps:
            continue
        yield {"net": net, "connectors": [f"{m['ref']}.{m['pin']}"
                                          for m in conns],
               "pull": (ohm, fv, pref, far), "readers": readers}
    yield {"checked": checked}


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


# ------------------------------------------------------ off-board inputs (24)

PIN_ABS_MAX = HERE.parent / "reference" / "pin_abs_max.yaml"
# a connector that leaves the board: J/P/CN/X/CON and a card edge (Z)
_OB_CONN_RE = re.compile(r"^(J|P|CN|X|CON|Z)\d")
# a board-to-board or programming header, not a cable to the outside world
_OB_HEADER_FP_RE = re.compile(
    r"(PinHeader|PinSocket|Pin_?Header|Socket_Strip|IDC|FFC|FPC|TestPoint|"
    r"FH\d\d|ZIF|Tag-Connect|TC20\d0|Pogo|Castellated|Module|Arduino|RaspberryPi|"
    r"Shield|Edge|M\.2|SODIMM|Card)", re.I)
_OB_USB_RE = re.compile(r"(USB|\bD[+-]|\bDP\b|\bDM\b|\bCC\d?\b|VBUS|SHIELD)",
                        re.I)
# an off-board line that reports state: someone else drives it, maybe open
# collector, and nothing on this board holds it
_OB_STATUS_RE = re.compile(r"(STATE|STATUS|DET(ECT)?|INT|IRQ|ALERT|"
                           r"FAULT|READY|RDY|BUSY|FLAG)")
_OB_WEAK_MARGIN = 0.5
_OB_ASSUMED_VDD = 3.3
_OB_SERIES_MIN = 100.0


def load_pin_abs_max(path: Path = PIN_ABS_MAX) -> dict:
    """reference/pin_abs_max.yaml as {receivers: [...], diodes: [...]}."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"pin abs max {path}: {exc}") from exc
    return {"receivers": doc.get("receivers") or [],
            "diodes": doc.get("diodes") or []}


def _abs_row(table: dict, comp: dict, m: dict) -> dict | None:
    """The pin_abs_max.yaml receiver row covering net member M, or None."""
    if _base_type(m.get("pintype")) in _POWER_TYPES:
        return None
    func = _norm_pin(m.get("pinfunction"))
    for r in table.get("receivers") or []:
        if not _spec_matches(r, comp):
            continue
        if r.get("pins") and not re.search(r["pins"], func, re.I):
            continue
        if r.get("pintypes") and not re.search(
                r["pintypes"], m.get("pintype") or "", re.I):
            continue
        return r
    return None


def _ob_solve(net: str, nets: dict, comps: dict,
              comp_pins: dict[str, dict[str, str]],
              rails: dict[str, float],
              absent: str | None = None) -> float | None:
    """DC volts on `net` from 2-pin resistors alone, every other pin high-Z
    and connectors open; the `absent` rail is open (a resistor to it leads
    nowhere). None: no resistive
    path to a known rail (the net floats) or the cluster is too large."""
    unknown, edges, seen, stamped = [], [], {net}, set()
    todo = [net]
    while todo:
        n = todo.pop()
        unknown.append(n)
        if len(unknown) > _DC_CLUSTER_MAX:
            return None
        for m in nets.get(n, []):
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            if _part_kind(ref) != "R" or len(pins) != 2 or ref in stamped:
                continue
            stamped.add(ref)
            ohm = engcalc.parse_value(comps.get(ref, {}).get("value"))
            far = next((x for p, x in pins.items() if p != m["pin"]), None)
            if not ohm or not far or far == n or far == absent:
                continue
            if far in rails:
                edges.append((n, far, float(ohm)))
            elif far.startswith("unconnected-"):
                continue
            else:
                edges.append((n, far, float(ohm)))
                if far not in seen:
                    seen.add(far)
                    todo.append(far)
    idx = {n: i for i, n in enumerate(unknown)}
    k = len(unknown)
    g = [[0.0] * (k + 1) for _ in range(k)]
    for a, b, ohm in edges:
        y = 1.0 / ohm
        for x, o in ((a, b), (b, a)):
            if x not in idx:
                continue
            g[idx[x]][idx[x]] += y
            if o in idx:
                g[idx[x]][idx[o]] -= y
            else:
                g[idx[x]][k] += y * rails[o]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(g[r][c]))
        if abs(g[piv][c]) < 1e-15:
            return None
        g[c], g[piv] = g[piv], g[c]
        for r in range(k):
            if r != c and g[r][c]:
                f = g[r][c] / g[c][c]
                g[r] = [x - f * y for x, y in zip(g[r], g[c])]
    return g[0][k] / g[0][0]


def _ob_clamps(net: str, nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]],
               rails: dict[str, float], diodes: list[dict]) -> list[dict]:
    """The diodes and protectors on `net`: one dict per part with 'side'
    ('low': it holds the net from going below its far rail, 'high': above),
    'reach' (the volts the net can get to past that rail through a listed
    silicon diode's vf_max, None for a part not listed: a Schottky, zener
    or TVS, taken to hold within the pin's rating) and 'row'."""
    out = []
    for m in nets.get(net, []):
        ref = m["ref"] or ""
        val = str(comps.get(ref, {}).get("value") or "")
        lib = (comps.get(ref, {}).get("libsource") or ("", ""))[1]
        pins = comp_pins.get(ref, {})
        if ref.startswith("U") and _CLAMP_VALUE_RE.search(val):
            out.append({"ref": ref, "side": "both", "reach": None,
                        "row": None})
            continue
        if not (ref.startswith(("D", "TVS", "ESD")) and not
                ref.startswith(("DS", "DISP"))) or "LED" in (lib + val).upper():
            continue
        if len(pins) != 2:
            out.append({"ref": ref, "side": "both", "reach": None,
                        "row": None})
            continue
        far = next((x for p, x in pins.items() if p != m["pin"]), None)
        fv = 0.0 if far and _is_ground_name(far) else rails.get(far)
        func = _norm_pin(m.get("pinfunction"))
        side = ("low" if func in ("K", "C", "CATHODE") else
                "high" if func in ("A", "ANODE") else "both")
        row = next((d for d in diodes if _spec_matches(d, comps[ref])), None)
        reach = None
        if row and fv is not None and side != "both":
            vf = float(row["vf_max"])
            reach = fv - vf if side == "low" else fv + vf
        out.append({"ref": ref, "side": side, "reach": reach, "row": row,
                    "far": far, "fv": fv})
    return out


def _offboard_inputs(nets: dict, comps: dict,
                     comp_pins: dict[str, dict[str, str]],
                     rails: dict[str, float], table: dict):
    """Yield one dict per off-board input fault (rule 28, class 3 of
    docs/miss-taxonomy.md); the last item is {'checked': n, 'pairs': [...]},
    the (net, receiver pin) pairs judged against a pin_abs_max.yaml row.

    An IC pin a receiver row covers is off-board when its net is on a
    connector, or one 2-pin resistor away from a connector net or from an
    off-board rail (a rail on a connector that no on-board part's power pin
    is on: the far system's +5V). Kinds:
      clamp  a listed silicon clamp diode is what holds the pin and its
             vf_max lets the pin past its rating, at a level the resistors
             reach with one off-board rail absent (or all present)
      rail   with an off-board rail absent the resistors take the pin past
             its rating and no clamp holds it within
      over   with every rail present the resistors take it past its rating
      bare   a cable connector (not a header or board-to-board part) that
             also carries a rail above the pin's rating, wired straight to a
             pin with 0.5 V or less of margin over its rail, with no clamp
             of any kind and no series resistor of 100R+
      float  a status-named connector net (STATE, DET, INT...) read by an
             IC input with no pull, no driver and no clamp, on a cable that
             also brings a rail above the reader's supply (not a host-bus
             edge): the far end is open collector at its own level, or
             unplugged, and the pin floats"""
    from check_ratings import volts_from_name  # noqa: PLC0415
    diodes = table.get("diodes") or []
    rails = dict(rails)
    # a negative rail named only on the connector's pin (Z101.A20 "-5V")
    for net, members in nets.items():
        if net in rails or net.startswith("unconnected-"):
            continue
        for m in members:
            if _OB_CONN_RE.match(m["ref"] or ""):
                pv = volts_from_name(_norm_pin(m.get("pinfunction")))
                if pv is not None and pv < 0:
                    rails[net] = pv
                    break

    def on_conn(net):
        return [m for m in nets.get(net, [])
                if _OB_CONN_RE.match(m["ref"] or "")]

    offrails = set()
    for net, v in rails.items():
        if v == 0 or _is_ground_name(net) or not on_conn(net):
            continue
        if not any(_base_type(m.get("pintype")) in _POWER_TYPES
                   and not _OB_CONN_RE.match(m["ref"] or "")
                   for m in nets.get(net, [])):
            offrails.add(net)

    def series_rs(net):
        for m in nets.get(net, []):
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            if _part_kind(ref) == "R" and len(pins) == 2:
                far = next((x for p, x in pins.items() if p != m["pin"]),
                           None)
                ohm = engcalc.parse_value(comps.get(ref, {}).get("value"))
                if far and ohm is not None:
                    yield ref, far, float(ohm)

    checked, pairs = 0, []
    for net, members in sorted(nets.items()):
        if (net.startswith("unconnected-") or _is_ground_name(net)
                or _supply_net(net, members, rails)):
            continue
        direct = on_conn(net)
        via = [(r, far, ohm) for r, far, ohm in series_rs(net)
               if on_conn(far) or far in offrails]
        status = None
        # a host bus edge (Z) drives its own lines (the ZX ULA's /INT)
        if (direct and _OB_STATUS_RE.search(net.upper())
                and not any((c["ref"] or "").startswith("Z") for c in direct)
                and not any(_OUTPUT_TOKEN_RE.match(t) for t in
                            re.split(r"[^A-Z0-9]+", net.upper()) if t)):
            status = net
        if not direct and not via:
            continue
        clamps = _ob_clamps(net, nets, comps, comp_pins, rails, diodes)
        pin_vmax = {}
        for m in members:
            ref = m["ref"] or ""
            if not ref.startswith("U") or ref not in comps:
                continue
            row = _abs_row(table, comps[ref], m)
            if not row:
                continue
            func = _norm_pin(m.get("pinfunction")) or m["pin"]
            vdd = row.get("vdd")
            if vdd is None and row.get("supply"):
                sup = [rails[n] for p, n in comp_pins.get(ref, {}).items()
                       if n in rails and re.search(
                           row["supply"], _norm_pin(next(
                               (x.get("pinfunction") for x in nets[n]
                                if x["ref"] == ref and x["pin"] == p), "")),
                           re.I)]
                vdd = max(sup) if sup else None
            vmax = (float(row["vmax"]) if row.get("vmax") is not None
                    else float(vdd) + float(row["vmax_vdd"])
                    if vdd is not None and row.get("vmax_vdd") is not None
                    else None)
            vmin = None if row.get("vmin") is None else float(row["vmin"])
            if vmax is None and vmin is None:
                continue
            pin_vmax[(ref, m["pin"])] = vmax
            checked += 1
            pairs.append((net, ref, m["pin"]))
            base = {"net": net, "receiver": ref, "rpin": m["pin"],
                    "rfunc": func, "row": row, "vmin": vmin, "vmax": vmax,
                    "vdd": vdd, "connectors": [f"{c['ref']}.{c['pin']}"
                                               for c in direct],
                    "via": [(r, far) for r, far, _ in via]}

            def beyond(level):
                return ((vmax is not None and level > vmax + 1e-3)
                        or (vmin is not None and level < vmin - 1e-3))

            def held(level):
                """(volts after the clamps, the clamp that set it)"""
                lo = [c for c in clamps if c["side"] in ("low", "both")]
                hi = [c for c in clamps if c["side"] in ("high", "both")]
                if vmin is not None and level < vmin and lo:
                    if any(c["reach"] is None for c in lo):
                        return vmin, None
                    c = max(lo, key=lambda c: c["reach"])
                    return max(level, c["reach"]), c
                if vmax is not None and level > vmax and hi:
                    if any(c["reach"] is None for c in hi):
                        return vmax, None
                    c = min(hi, key=lambda c: c["reach"])
                    return min(level, c["reach"]), c
                return level, None

            scen = [(None, _ob_solve(net, nets, comps, comp_pins, rails))]
            for r in sorted(offrails):
                scen.append((r, _ob_solve(net, nets, comps, comp_pins,
                                          rails, absent=r)))
            seen_kinds = set()
            for gone, level in scen:
                if level is None or not beyond(level):
                    continue
                after, clamp = held(level)
                if not beyond(after):
                    continue
                kind = ("clamp" if clamp else "over" if gone is None
                        else "rail")
                if clamp and gone is not None and "rail" not in seen_kinds:
                    seen_kinds.add("rail")
                    yield dict(base, kind="rail", absent=gone, level=level,
                               after=after, clamp=clamp)
                if kind in seen_kinds:
                    continue
                seen_kinds.add(kind)
                yield dict(base, kind=kind, absent=gone, level=level,
                           after=after, clamp=clamp)
            # bare: a cable connector straight onto a weak pin
            margin = (float(row["vmax_vdd"]) if row.get("vmax_vdd")
                      is not None else (vmax - float(vdd))
                      if vmax is not None and vdd is not None else None)
            # the far system's own rail on the same cable sets the level
            # a fault on it can put on the pin (smart-lcc's +12V on J1)
            cables = [c for c in direct if not _OB_HEADER_FP_RE.search(
                str(comps.get(c["ref"], {}).get("footprint") or ""))
                and not _OB_CONN_RE.match(c["ref"]).group(1) == "Z"
                and vmax is not None
                and any(rails.get(n, 0) > vmax for n in
                        comp_pins.get(c["ref"], {}).values())]
            if (cables and margin is not None and margin <= _OB_WEAK_MARGIN
                    and not clamps and not _OB_USB_RE.search(net)
                    and not _OB_USB_RE.search(func)):
                hot = sorted({n for c in cables for n in comp_pins.get(
                    c["ref"], {}).values() if rails.get(n, 0) > vmax})
                yield dict(base, kind="bare", margin=margin, hot=hot,
                           cables=[f"{c['ref']}.{c['pin']}" for c in cables])
        # float: a status line nothing on this board holds
        if status:
            pulls = [1 for r, far, _ in series_rs(net)
                     if _is_ground_name(far) or far in rails]
            drivers = [x for x in members
                       if not _OB_CONN_RE.match(x["ref"] or "")
                       and (_base_type(x.get("pintype")) in _OB_DRIVE_TYPES
                            or (x["ref"] or "").startswith("Q"))]
            readers = [x for x in members if (x["ref"] or "").startswith("U")
                       and _base_type(x.get("pintype")) in (
                           "input", "bidirectional")]
            # an internal pull at the reader's own level serves a line
            # whose far end idles there; the cable that brings a higher
            # rail (fc980c's +5V keyboard) needs a pull at that level, and
            # a host-bus edge (an Apple II or ZX slot) holds its own lines
            def supply(ref):
                vs = [rails[n] for n, ms in nets.items() if n in rails
                      and rails[n] > 0 and not _is_ground_name(n)
                      for y in ms if y["ref"] == ref
                      and _base_type(y.get("pintype")) in _POWER_TYPES]
                return max(vs) if vs else None
            rvdd = [supply(x["ref"]) for x in readers]
            rvdd = max((v for v in rvdd if v is not None), default=None)
            # a reader whose supply net carries no voltage (fc980c's
            # STM32F401 on 'VDDA') is taken as 3.3 V logic
            seen = rvdd if rvdd is not None else _OB_ASSUMED_VDD
            hot = sorted({n for c in direct
                          for n in comp_pins.get(c["ref"], {}).values()
                          if rails.get(n, 0) > seen + 0.5})
            higher = bool(hot)
            edge = any("EDGE" in str(comps.get(c["ref"], {}).get(
                "footprint") or "").upper() for c in direct)
            # a reader rated below the cable's rail (glasgow's ECP5 bank on
            # a +5V port) can't be pulled to it: the only pull that works is
            # one at its own rail, which its gateware or firmware sets
            top = max((rails[n] for n in hot), default=0.0)
            lims = [pin_vmax.get((x["ref"], x["pin"])) for x in readers]
            own = bool(lims) and all(v is not None and v < top for v in lims)
            if (readers and higher and not edge and not pulls
                    and not drivers and not clamps):
                yield {"net": net, "kind": "float", "hot": hot, "rvdd": rvdd,
                       "own_rail": own, "top": top,
                       "connectors": [f"{c['ref']}.{c['pin']}"
                                      for c in direct],
                       "readers": [f"{x['ref']}."
                                   f"{_norm_pin(x.get('pinfunction')) or x['pin']}"
                                   for x in readers], "row": None}
    yield {"checked": checked, "pairs": pairs}


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
    for i, ent in enumerate(constraints.get("switched_loads", []) or []):
        if not isinstance(ent, dict):
            continue
        add(ent.get("net"), f"switched_loads[{i}].net")
        for j, n in enumerate(ent.get("nets") or []):
            add(n, f"switched_loads[{i}].nets[{j}]")
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


# refs and libraries whose symbols are generic: their Value names no part
_GENERIC_REF_RE = re.compile(r"^(J|P|CN|X|TP|MH|H|SW|JP|BT|FID)\d")
_GENERIC_LIB_RE = re.compile(r"^(Device|power|Connector.*|Mechanical|Jumper|"
                             r"Switch|Graphic|Simulation_SPICE|pspice)$")


def _symbol_part(ref: str, comp: dict) -> tuple[str, str] | None:
    """(lib, symbol) of a placed part the symbol rules judge, else None."""
    lib, part = comp.get("libsource") or ("", "")
    if not part or _GENERIC_REF_RE.match(ref) or _GENERIC_LIB_RE.match(lib):
        return None
    return lib, part


def _placed_pinmap(ref: str, comp: dict, parsed: dict
                   ) -> dict[str, str]:
    """REF's symbol pins {number: name}: its libpart's, else the names its
    net nodes give."""
    lp = parsed.get("libpart_pins", {}).get(comp.get("libsource") or ())
    if lp:
        return dict(lp)
    out = {}
    for members in parsed["nets"].values():
        for m in members:
            if m["ref"] == ref and m["pin"]:
                out.setdefault(m["pin"], m.get("pinfunction") or "")
    return out


def _table_pinmap(row: dict, symdir) -> dict[str, str] | None:
    """A part_pinouts.yaml row's pins {number: name}: its own `pins`, or
    the pins of the stock symbol its `stock` names (a module whose
    stock symbol follows the module's own pad numbers)."""
    if row.get("pins"):
        return {str(k): str(v) for k, v in row["pins"].items()}
    lib, _, name = str(row.get("stock") or "").partition(":")
    if not (symdir and name):
        return None
    for slib, sname, path in pinmaplib.stock_index(str(symdir))[
            "exact"].get(name.upper(), []):
        if slib == lib:
            st = pinmaplib.stock_symbol(path)
            return dict(st["pins"]) if st else None
    return None


def _symbol_pinmap_hits(parsed: dict, symdir, pinouts: list[dict] | None = None
                        ) -> tuple[list, int, int]:
    """[(ref, (lib, symbol), stock hit, diffs, table row)] for each placed
    part whose Value or MPN names a stock KiCad part, in the same package,
    whose pin map disagrees with the placed symbol's; how many parts were
    compared; and how many of those were compared with a part_pinouts.yaml
    row. A stock symbol's package is its default Footprint, else its
    ki_fp_filters (pinmaplib.stock_package_match): a PLCC-44 part is not
    compared with a DIP-40-numbered stock symbol, and one whose package
    can't be told among several stock packages is not judged. A part
    some same-package stock candidate agrees with is quiet.
    One with no same-package stock symbol is compared with the first
    pinouts row whose `match` takes its Value, MPN or symbol name and
    whose `footprint` takes its footprint (a pad numbering is a
    footprint's), when the two maps share at least 3 pin names; the hit's
    table row is that row (None for a stock hit)."""
    hits, checked, by_table = [], 0, 0
    comps = parsed["components"]
    for ref in sorted(comps):
        c = comps[ref]
        sp = _symbol_part(ref, c)
        if not sp:
            continue
        placed = _placed_pinmap(ref, c, parsed)
        if len(placed) < 3:
            continue
        value = (c.get("value") or "").strip()
        names = [value, value.split()[0] if value else "", c.get("mpn") or "",
                 sp[1]]
        cands = [h for h in pinmaplib.stock_candidates(names, symdir)
                 if (h[0], h[1]) != sp]
        same_pkg, unsure = [], False
        for lib, name, path in cands:
            st = pinmaplib.stock_symbol(path)
            if not st or len(st["pins"]) < 3:
                continue
            fits = pinmaplib.stock_package_match(c.get("footprint"), st)
            unsure |= fits is None
            if fits:
                same_pkg.append(((lib, name, st),
                                 pinmaplib.pinmap_diffs(placed, st["pins"])))
        # the board's package unknown and the stock symbols drawn for
        # several: which one's numbering holds can't be told (not judged)
        if pinmaplib.package_family(c.get("footprint")) is None and len(
                {pinmaplib.package_family(h[0][2]["footprint"])
                 for h in same_pkg} - {None}) > 1:
            unsure = True
        if same_pkg and unsure and all(d for _, d in same_pkg):
            continue
        if same_pkg:
            checked += 1
            if all(d for _, d in same_pkg):
                (hit, diffs) = same_pkg[0]
                hits.append((ref, sp, hit, diffs, None))
            continue
        row = pinmaplib.pinout_row(pinouts or [], names, c.get("footprint"))
        pins = _table_pinmap(row, symdir) if row else None
        if not pins or len(set(pinmaplib.by_name(placed))
                           & set(pinmaplib.by_name(pins))) < 3:
            continue
        checked += 1
        by_table += 1
        diffs = pinmaplib.pinmap_diffs(placed, pins)
        if diffs:
            hits.append((ref, sp, ("part_pinouts", row["part"],
                                   {"footprint": row.get("footprint"),
                                    "pins": pins}), diffs, row))
    return hits, checked, by_table


def _mcu_i2c_swaps(nets: dict, comps: dict,
                   comp_pins: dict[str, dict[str, str]], table: dict
                   ) -> tuple[list, int]:
    """[(mcu row, ref, block, [(pin, name, function, net, role)])] for each
    MCU I2C block whose SDA pin sits on the board's SCL net and whose SCL
    pin on its SDA net (mcu_pins.yaml i2c tables); and how many MCUs with
    an i2c table were placed. A net's role is its name (OLED_SDA; SCK or
    CLK beside an SDA of the same stem), else an SDA/SCL pin of another
    part on it."""
    rows = [r for r in (table.get("mcus") or []) if r.get("i2c")]
    roles = pinmaplib.i2c_net_roles(nets)
    for name, members in nets.items():
        if name in roles:
            continue
        for m in members:
            tok = _bus_token(m.get("pinfunction") or "")
            if tok in ("SDA", "SCL"):
                roles[name] = tok
                break
    out, checked = [], 0
    for ref in sorted(comps):
        row = _mcu_row(comps[ref], rows) if rows else None
        if not row:
            continue
        checked += 1
        blocks: dict[str, dict[str, tuple]] = {}
        for func, (pnum, net) in _pin_funcs(ref, nets, comp_pins).items():
            f = pinmaplib.mcu_i2c_function(func, row["i2c"])
            if not f or net not in roles:
                continue
            block, line = f.rsplit("_", 1)
            blocks.setdefault(block, {})[line] = (pnum, func, f, net,
                                                  roles[net])
        for block, lines in sorted(blocks.items()):
            sda, scl = lines.get("SDA"), lines.get("SCL")
            if sda and scl and sda[4] == "SCL" and scl[4] == "SDA":
                out.append((row, ref, block, [sda, scl]))
    return out, checked


SOURCE_LIMITS = HERE.parent / "reference" / "source_limits.yaml"
# a part that takes no supply current worth counting, or passes the rail
# through (passives, test points, jumpers, mounting holes, fiducials)
_SL_PASSIVE_RE = re.compile(r"^(R|RN|C|L|FB|D|LED|TP|JP|SJ|H|MH|FID|SW|F|"
                            r"Y|X|Q|NT)\d")
_SL_LED_RE = re.compile(r"LED", re.I)
_SL_VF_RE = re.compile(r"(?<![\d.])(\d(?:\.\d+)?)\s*V(?![A-Za-z0-9])")
# a USB power-delivery or charger-detect controller: the port may give more
# than the default current, so the VBUS budget is not judged
_SL_PD_RE = re.compile(r"^(STUSB45|FUSB30|TPS2575|TPS6598|CYPD|HUSB23|CH22[14]|"
                       r"IP272|AP3377|RT1715|TCPP0|PTN5110|BQ2589|BQ2560|"
                       r"MAX7748|TUSB32)", re.I)
_SL_SS_RE = re.compile(r"^(SSTX|SSRX|TX\d?[+-]|RX\d?[+-]|STDA_SS|SSTX[+-]|"
                       r"SS_?[TR]X)", re.I)
_SL_CC_RE = re.compile(r"^CC\d?$", re.I)


def load_source_limits(path: Path = SOURCE_LIMITS) -> dict:
    """reference/source_limits.yaml as {regulators, loads, gpio,
    converters, usb}."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"source limits {path}: {exc}") from exc
    return {"regulators": doc.get("regulators") or [],
            "loads": doc.get("loads") or [],
            "gpio": doc.get("gpio") or [],
            "converters": doc.get("converters") or [],
            "usb": doc.get("usb") or {"usb2_a": 0.5, "usb3_a": 0.9}}


def _sl_row(rows: list[dict], comp: dict) -> dict | None:
    if comp.get("dnp"):
        return None
    return next((r for r in rows if _spec_matches(r, comp)), None)


def _sl_led_vf(comp: dict) -> float | None:
    """An LED's forward voltage, only when its value says it (2.65V)."""
    m = _SL_VF_RE.search(comp.get("value") or "")
    return float(m.group(1)) if m else None


def _sl_is_led(ref: str, comp: dict) -> bool:
    return bool(re.match(r"^(D|LED)\d", ref) and _SL_LED_RE.search(
        (comp.get("value") or "") + " "
        + (comp.get("libsource") or ("", ""))[1]))


def _sl_led_chains(net: str, nets: dict, comps: dict,
                   comp_pins: dict[str, dict[str, str]], rails: dict
                   ) -> list[dict]:
    """Resistor + LED chains from NET to a net of known volts: NET-R-LED-far
    or NET-LED-R-far. Each: {r, led, ohms, vf (or None), far, far_v}."""
    out = []
    for m in nets.get(net, []):
        a = m["ref"]
        pins = comp_pins.get(a, {})
        if len(pins) != 2 or comps.get(a, {}).get("dnp"):
            continue
        mid = next((n for p, n in pins.items() if p != m["pin"]), None)
        if not mid or mid == net:
            continue
        for m2 in nets.get(mid, []):
            b = m2["ref"]
            if b == a or len(comp_pins.get(b, {})) != 2 \
                    or comps.get(b, {}).get("dnp"):
                continue
            far = next((n for p, n in comp_pins[b].items()
                        if p != m2["pin"]), None)
            if far is None or far == mid or far not in rails:
                continue
            kinds = {_part_kind(a), _part_kind(b)}
            led = a if _sl_is_led(a, comps[a]) else (
                b if _sl_is_led(b, comps[b]) else None)
            r = b if led == a else a
            if led is None or _part_kind(r) != "R" or "R" not in kinds:
                continue
            ohms = engcalc.parse_value(comps[r].get("value"))
            if not ohms:
                continue
            out.append({"r": r, "led": led, "ohms": float(ohms),
                        "vf": _sl_led_vf(comps[led]), "far": far,
                        "far_v": rails[far]})
    return out


def _sl_rail_load(net: str, nets: dict, comps: dict,
                  comp_pins: dict[str, dict[str, str]], table: dict,
                  rails: dict, skip: set[str], _depth: int = 0,
                  given: dict[str, float] | None = None) -> dict:
    """What hangs on rail NET (and the nets one 0 ohm resistor away): the
    current its known loads draw (peak: every row; cont: rows not marked
    peak, LED chains, downstream LDOs' own continuous load), the parts it
    can't tell, the rows the sum rests on, and the refs it counted. GIVEN
    {ref: amps} (a load's stated current, check_thermal) outranks a row."""
    given = given or {}
    res = {"peak": 0.0, "cont": 0.0, "known": [], "unknown": [],
           "rows": [], "refs": []}
    reach = _zero_ohm_reach(net, nets, comps, comp_pins)
    seen = set(skip)
    v_rail = next((rails[n] for n in reach if n in rails), None)
    for n in reach:
        for m in nets.get(n, []):
            ref = m["ref"]
            comp = comps.get(ref, {})
            if not ref or ref in seen or comp.get("dnp"):
                continue
            if ref in given:
                seen.add(ref)
                i = float(given[ref])
                res["peak"] += i
                res["cont"] += i
                res["refs"].append(ref)
                res["known"].append(f"{ref} ({comp.get('value')}) "
                                    f"{i * 1000:g} mA stated")
                continue
            func = _norm_pin(m.get("pinfunction"))
            reg = _sl_row(table["regulators"], comp)
            if reg and re.search(reg["in"], func, re.I):
                seen.add(ref)
                outs = {nn for p, nn in comp_pins.get(ref, {}).items()
                        if re.search(reg["out"], _norm_pin(
                            _pin_func(nets, ref, p, nn)), re.I)}
                if reg.get("kind") != "ldo" or not outs or _depth > 3:
                    res["unknown"].append(f"{ref} ({comp.get('value')})")
                    continue
                sub = _sl_rail_load(sorted(outs)[0], nets, comps, comp_pins,
                                    table, rails, seen | {ref}, _depth + 1,
                                    given)
                seen |= {ref}
                res["peak"] += sub["peak"]
                res["cont"] += sub["cont"]
                res["rows"] += [reg] + sub["rows"]
                res["refs"] += [ref] + sub["refs"]
                res["unknown"] += sub["unknown"]
                if sub["peak"]:
                    res["known"].append(
                        f"{ref} ({comp.get('value')}) passing on "
                        f"{sub['peak']:.3g} A")
                continue
            load = _sl_row(table["loads"], comp)
            if load and re.search(load["supply"], func, re.I):
                seen.add(ref)
                i = float(load["i_supply"])
                res["peak"] += i
                if not load.get("peak"):
                    res["cont"] += i
                res["rows"].append(load)
                res["refs"].append(ref)
                res["known"].append(f"{ref} ({comp.get('value')}) "
                                    f"{i * 1000:g} mA")
                continue
            if _base_type(m.get("pintype")) == "power_out" or reg:
                continue
            if _SL_PASSIVE_RE.match(ref):
                continue
            if _CONNECTOR_RE.match(ref) or _base_type(
                    m.get("pintype")) == "power_in":
                seen.add(ref)
                res["unknown"].append(f"{ref} ({comp.get('value')})")
        if v_rail is None:
            continue
        for ch in _sl_led_chains(n, nets, comps, comp_pins, rails):
            if ch["r"] in seen or ch["led"] in seen or ch["far_v"] != 0:
                continue
            seen |= {ch["r"], ch["led"]}
            if ch["vf"] is None:
                res["unknown"].append(f"{ch['led']} (an LED with no Vf "
                                      f"in its value)")
                continue
            i = max(0.0, (v_rail - ch["vf"]) / ch["ohms"])
            res["peak"] += i
            res["cont"] += i
            res["refs"] += [ch["r"], ch["led"]]
            res["known"].append(f"{ch['led']} via {ch['r']} "
                                f"{i * 1000:.3g} mA")
    return res


def _pin_func(nets: dict, ref: str, pin: str, net: str) -> str:
    return next((m.get("pinfunction") or "" for m in nets.get(net, [])
                 if m["ref"] == ref and m["pin"] == pin), "")


def _sl_judged(rows: list[dict]) -> bool:
    return all(r.get("verified") is True for r in rows)


def _sl_src(rows: list[dict]) -> str:
    seen = []
    for r in rows:
        s = " ".join(str(r.get("source", r.get("mpn"))).split())
        if s not in seen:
            seen.append(s)
    return "; ".join(seen)


_SW_FET_RE = re.compile(r"^Q\d")


def _sw_ch(func: str | None) -> str | None:
    """A FET pin's channel side: D or S (None for a gate or other pin)."""
    f = _norm_pin(func)
    return {"D": "D", "DRAIN": "D", "S": "S", "SOURCE": "S"}.get(f)


def _sw_node(net: str, nets: dict, comps: dict,
             comp_pins: dict[str, dict[str, str]]) -> set[str]:
    """NET and the nets a FET's same-side channel pins tie it to (a FET
    whose drain pins were drawn on two nets)."""
    node, todo = {net}, [net]
    while todo:
        n = todo.pop()
        for m in nets.get(n, []):
            ref, ch = m["ref"], _sw_ch(m.get("pinfunction"))
            if not ch or not _SW_FET_RE.match(ref) \
                    or comps.get(ref, {}).get("dnp"):
                continue
            for p, nn in comp_pins.get(ref, {}).items():
                if nn not in node and not nn.startswith("unconnected-") \
                        and _sw_ch(_pin_func(nets, ref, p, nn)) == ch:
                    node.add(nn)
                    todo.append(nn)
    return node


def _sw_high_side(node: set[str], nets: dict, comps: dict,
                  comp_pins: dict[str, dict[str, str]], rails: dict
                  ) -> list[tuple[str, str]]:
    """(fet, rail) for each FET with a channel pin on NODE whose other
    channel pin sits on a net outside it that is not 0 V."""
    out = []
    for n in sorted(node):
        for m in nets.get(n, []):
            ref, ch = m["ref"], _sw_ch(m.get("pinfunction"))
            if not ch or not _SW_FET_RE.match(ref) \
                    or comps.get(ref, {}).get("dnp"):
                continue
            for p, nn in sorted(comp_pins.get(ref, {}).items()):
                other = _sw_ch(_pin_func(nets, ref, p, nn))
                if other and other != ch and nn not in node \
                        and not nn.startswith("unconnected-") \
                        and rails.get(nn) != 0 \
                        and (ref, nn) not in out:
                    out.append((ref, nn))
    return out


def _sw_drive(net: str, nets: dict, comps: dict,
              comp_pins: dict[str, dict[str, str]], rails: dict
              ) -> tuple[str, str, str | None, float] | str:
    """How load net NET is switched: (fet, rail, series resistor or None,
    its ohms), or a note saying why it can't tell."""
    hs = _sw_high_side(_sw_node(net, nets, comps, comp_pins), nets, comps,
                       comp_pins, rails)
    if hs:
        return hs[0][0], hs[0][1], None, 0.0
    through, r_hits = [], []
    for m in nets.get(net, []):
        ref = m["ref"]
        pins = comp_pins.get(ref, {})
        if len(pins) != 2 or comps.get(ref, {}).get("dnp"):
            continue
        far = next((n for p, n in pins.items() if p != m["pin"]), None)
        kind = _part_kind(ref)
        if not far or far == net or kind not in ("R", "L"):
            continue
        if kind == "L":
            through.append(f"{ref} ({comps[ref].get('value')})")
            continue
        hs = _sw_high_side(_sw_node(far, nets, comps, comp_pins), nets,
                           comps, comp_pins, rails)
        ohms = engcalc.parse_value(comps[ref].get("value"))
        if hs and ohms:
            r_hits.append((hs[0][0], hs[0][1], ref, float(ohms)))
    if through:   # an inductor's own current rises at V/L, not an edge
        return ("it is driven through " + ", ".join(through)
                + ", not switched hard from a rail")
    if r_hits:
        return min(r_hits, key=lambda h: h[3])
    return "no FET switches it from a rail"


def _sw_converter(rail: str, nets: dict, comps: dict,
                  comp_pins: dict[str, dict[str, str]], rows: list[dict],
                  rails: dict) -> tuple[str, dict, float | None] | None:
    """(converter ref, its row, volts its FB divider sets) for the
    converter with a source_limits converters row that regulates RAIL:
    an FB pin whose divider's top resistor is on RAIL."""
    for ref in sorted(comps):
        row = _sl_row(rows, comps[ref])
        if not row:
            continue
        for p, fbn in comp_pins.get(ref, {}).items():
            if not re.search(row["fb"], _norm_pin(
                    _pin_func(nets, ref, p, fbn)), re.I):
                continue
            top = bot = None
            for m in nets.get(fbn, []):
                r = m["ref"]
                pins = comp_pins.get(r, {})
                if _part_kind(r) != "R" or len(pins) != 2 \
                        or comps.get(r, {}).get("dnp"):
                    continue
                far = next((n for pp, n in pins.items() if pp != m["pin"]),
                           None)
                ohms = engcalc.parse_value(comps[r].get("value"))
                if far == rail and ohms:
                    top = float(ohms)
                elif far is not None and rails.get(far) == 0 and ohms:
                    bot = float(ohms)
            if top is not None:
                v = (float(row["vref"]) * (1 + top / bot) if bot else None)
                return ref, row, v
    return None


def _sw_cap_on(rail: str, nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]], rails: dict) -> float:
    """Farads of the capacitors from RAIL to a 0 V net."""
    tot = 0.0
    for m in nets.get(rail, []):
        ref = m["ref"]
        pins = comp_pins.get(ref, {})
        if _part_kind(ref) != "C" or len(pins) != 2 \
                or comps.get(ref, {}).get("dnp"):
            continue
        far = next((n for p, n in pins.items() if p != m["pin"]), None)
        f = engcalc.parse_value(comps[ref].get("value"))
        if far is not None and rails.get(far) == 0 and f:
            tot += float(f)
    return tot


def _switched_loads(nets: dict, comps: dict,
                    comp_pins: dict[str, dict[str, str]], table: dict,
                    rails: dict, loads: list):
    """switched_load_droop findings for constraints `switched_loads`, in
    _source_limits's form, then ("checked", n)."""
    checked = 0
    rows = table.get("converters") or []
    for i, ent in enumerate(loads or []):
        if not isinstance(ent, dict):
            continue
        lnets = ent.get("nets") or ([ent["net"]] if ent.get("net") else [])
        c_l = ent.get("capacitance_f")
        lnets = [n for n in lnets if isinstance(n, str) and n in nets]
        if not lnets or not isinstance(c_l, (int, float)) or c_l <= 0:
            continue
        where = " / ".join(lnets)
        drives = [_sw_drive(n, nets, comps, comp_pins, rails)
                  for n in lnets]
        notes = [d for d in drives if isinstance(d, str)]
        if notes:
            yield ("not_judged",
                   f"switched_load_droop on {where}: {notes[0]}, so the "
                   f"{c_l * 1e6:.3g} uF load is not judged against a "
                   f"converter's limit")
            continue
        rail_set = {d[1] for d in drives}
        if len(rail_set) != 1:
            yield ("not_judged",
                   f"switched_load_droop on {where}: the load nets are "
                   f"switched from different rails ({', '.join(sorted(rail_set))})")
            continue
        rail = rail_set.pop()
        conv = _sw_converter(rail, nets, comps, comp_pins, rows, rails)
        if conv is None:
            yield ("not_judged",
                   f"switched_load_droop on {where}: no converters row in "
                   f"reference/source_limits.yaml regulates '{rail}', so "
                   f"its limits are not known")
            continue
        cref, row, v_fb = conv
        v = rails.get(rail, v_fb)
        c_out = _sw_cap_on(rail, nets, comps, comp_pins, rails)
        if not v or v <= 0 or not c_out:
            yield ("not_judged",
                   f"switched_load_droop on {where}: '{rail}' has "
                   f"{'no volts hwde can read' if not v else 'no output capacitance'}")
            continue
        checked += 1
        swing = v * len(lnets)          # a bridge across two legs: -V to +V
        rs = sum(d[3] for d in drives)
        ilim = float(row["ilim_min"])
        droop = c_l * swing / (c_out + c_l) / v
        uv = 1 - float(row["uv_pct"]) / 100
        if droop <= uv or (rs and swing / rs <= ilim):
            continue
        fets = sorted({d[0] for d in drives})
        q = c_l * swing
        how = ("the constraints" if rail in rails
               else f"{cref}'s FB divider")
        series = ("" if not rs else
                  f" Its {rs:g} ohm series resistance lets {swing / rs:.3g} "
                  f"A through, past the limit.")
        yield ("switched_load_droop", "error", rail,
               [cref] + fets, (
                   f"{', '.join(fets)} switch the {c_l * 1e6:.3g} uF load "
                   f"on '{where}' (capacitance_f in the constraints) from "
                   f"'{rail}' ({v:.3g} V by {how}), {swing:.3g} V "
                   f"each edge{' (a bridge: -V to +V)' if len(lnets) > 1 else ''}: it takes {q * 1e3:.3g} mC, "
                   f"and the {c_out * 1e6:.3g} uF on '{rail}' falls "
                   f"{droop * 100:.0f}%, past the {uv * 100:.0f}% drop at "
                   f"which {cref} ({comps[cref].get('value')}) takes its "
                   f"output as undervoltage. Refilling that charge at its "
                   f"{ilim:g} A minimum switch current limit takes at least "
                   f"{q / ilim * 1e6:.3g} us in current limit, and "
                   f"{row['hiccup_cycles']} cycles there start hiccup."
                   f"{series} Put at least {swing / ilim:.3g} ohm in "
                   f"series with the load, add output capacitance, or "
                   f"drive it with a current-limited stage "
                   f"[{_sl_src([row])}]"),
               [row], {"droop_pct": round(droop * 100, 1),
                       "load_f": c_l, "cout_f": round(c_out, 9),
                       "rail_v": round(v, 3)})
    yield ("checked", checked)


def _source_limits(nets: dict, comps: dict,
                   comp_pins: dict[str, dict[str, str]], table: dict,
                   rails: dict):
    """Rule 32's findings: (kind, sev, net, refs, msg, rows, extra) and
    ("not_judged", msg) notes, then ("checked", n)."""
    from check_ratings import reg_out_volts  # noqa: PLC0415
    checked = 0
    # rail_over / ldo_heat
    for ref in sorted(comps):
        comp = comps[ref]
        reg = _sl_row(table["regulators"], comp)
        if not reg:
            continue
        pins = comp_pins.get(ref, {})
        outs = sorted({n for p, n in pins.items() if re.search(
            reg["out"], _norm_pin(_pin_func(nets, ref, p, n)), re.I)})
        ins = sorted({n for p, n in pins.items() if re.search(
            reg["in"], _norm_pin(_pin_func(nets, ref, p, n)), re.I)})
        if not outs or outs[0].startswith("unconnected-"):
            continue
        checked += 1
        out = outs[0]
        ld = _sl_rail_load(out, nets, comps, comp_pins, table, rails, {ref})
        lim = float(reg["iout_max"])
        rows = [reg] + ld["rows"]
        val = comp.get("value")
        unk = (f"; it can't tell {len(ld['unknown'])} more "
               f"({', '.join(ld['unknown'][:4])}"
               f"{', ...' if len(ld['unknown']) > 4 else ''})"
               if ld["unknown"] else "")
        if ld["peak"] > lim + 1e-9:
            yield ("rail_over", "error", out, [ref], (
                f"{ref} ({val}) is rated for {lim * 1000:g} mA out, but "
                f"the loads on '{out}' it can name ask "
                f"{ld['peak'] * 1000:.3g} mA: {', '.join(ld['known'])}"
                f"{unk}. Fit a regulator rated for the sum, or split the "
                f"rail [{_sl_src(rows)}]"),
                rows, {"limit_a": lim, "load_a": round(ld["peak"], 4)})
        elif ld["unknown"]:
            yield ("not_judged",
                   f"rail_over on {out}: {ref} ({val}) is rated "
                   f"{lim * 1000:g} mA; the loads with a datasheet row ask "
                   f"{ld['peak'] * 1000:.3g} mA, and "
                   f"{len(ld['unknown'])} load(s) have no current row "
                   f"({', '.join(ld['unknown'][:4])})")
        vout = rails.get(out, reg_out_volts(str(val or "")))
        vin = next((rails[n] for n in ins if n in rails), None)
        if not ld["cont"]:
            continue
        if vin is None or vout is None:
            yield ("not_judged",
                   f"ldo_heat on {out}: {ref} ({val}) passes "
                   f"{ld['cont'] * 1000:.3g} mA continuous, but its "
                   f"{'input' if vin is None else 'output'} volts can't "
                   f"be read from the net names")
            continue
        p = max(0.0, vin - vout) * ld["cont"]
        if reg.get("theta_ja") is None or reg.get("tj_max") is None:
            # some datasheets (Torex XC6206) give only package Pd, no
            # junction-to-ambient figure or junction limit to judge by
            yield ("not_judged",
                   f"ldo_heat on {out}: {ref} ({val}) drops "
                   f"{p:.3g} W, but its datasheet gives no theta_ja or "
                   f"junction limit [{reg.get('source', '')}]")
            continue
        tj = 25 + p * float(reg["theta_ja"])
        if tj > float(reg["tj_max"]):
            yield ("ldo_heat", "error", out, [ref], (
                f"{ref} ({val}) drops {vin:g} V to {vout:g} V at "
                f"{ld['cont'] * 1000:.3g} mA continuous "
                f"({', '.join(ld['known'])}): {p:.3g} W, which at its "
                f"best package's {float(reg['theta_ja']):g} °C/W puts the "
                f"junction at {tj:.0f} °C from 25 °C ambient, past its "
                f"{float(reg['tj_max']):g} °C. Drop the input volts, use a "
                f"switcher, or cut the load [{_sl_src(rows)}]"),
                rows, {"power_w": round(p, 3), "tj_c": round(tj, 1)})
    # gpio_over
    for ref in sorted(comps):
        comp = comps[ref]
        row = _sl_row(table["gpio"], comp)
        if not row:
            continue
        pins = comp_pins.get(ref, {})
        vio = next((rails[n] for p, n in pins.items() if n in rails
                    and rails[n] > 0 and re.search(
                        row["supply"], _norm_pin(_pin_func(nets, ref, p, n)),
                        re.I)), None)
        if vio is None:
            continue
        checked += 1
        src_sum = sink_sum = 0.0
        hits, vague = [], []
        for p, n in sorted(pins.items()):
            func = _norm_pin(_pin_func(nets, ref, p, n))
            if not re.search(row["pins"], func, re.I):
                continue
            for ch in _sl_led_chains(n, nets, comps, comp_pins, rails):
                sink = ch["far_v"] > 0
                drive = ch["far_v"] if sink else vio
                hi = drive / ch["ohms"]
                i = (None if ch["vf"] is None
                     else max(0.0, (drive - ch["vf"]) / ch["ohms"]))
                if i is None:
                    vague.append((func, ch, hi))
                    i_low = 0.0
                else:
                    i_low = i
                if sink:
                    sink_sum += i_low
                else:
                    src_sum += i_low
                pmax = row.get("pin_max")
                if pmax is None:
                    continue
                if i is not None and i > float(pmax) + 1e-9:
                    hits.append((func, n, ch, i))
                elif i is None and hi > float(pmax) + 1e-9:
                    yield ("not_judged",
                           f"gpio_over on {n}: {ref} {func} drives "
                           f"{ch['led']} through {ch['r']} "
                           f"({ch['ohms']:g} ohm), up to "
                           f"{hi * 1000:.3g} mA against "
                           f"{float(pmax) * 1000:g} mA, but the LED's Vf "
                           f"isn't in its value")
        src = " ".join(row["source"].split())
        for func, n, ch, i in hits:
            yield ("gpio_over", "error", n, [ref, ch["r"], ch["led"]], (
                f"{ref} ({comp.get('value')}) {func} drives {ch['led']} "
                f"through {ch['r']} ({ch['ohms']:g} ohm) at "
                f"{i * 1000:.3g} mA, past the {row['pin_max'] * 1000:g} mA "
                f"one pin may carry. Raise {ch['r']} or drive the LED "
                f"through a transistor [{src}]"),
                [row], {"pin": func, "current_a": round(i, 4)})
        tmax = row.get("total_max")
        for what, s in (("source", src_sum), ("sink", sink_sum)):
            if tmax is not None and s > float(tmax) + 1e-9:
                yield ("gpio_total_over", "warning", None, [ref], (
                    f"{ref} ({comp.get('value')})'s pins {what} "
                    f"{s * 1000:.3g} mA into LEDs together, past the "
                    f"{float(tmax) * 1000:g} mA the part allows in all. "
                    f"Fine only if they are never all on at once; "
                    f"otherwise raise the resistors [{src}]"),
                    [row], {"current_a": round(s, 4)})
    # vbus_over
    if any(_SL_PD_RE.match(c.get("value") or "")
           for c in comps.values() if not c.get("dnp")):
        yield ("checked", checked)
        return
    usb = table.get("usb") or {}
    for ref in sorted(comps):
        comp = comps[ref]
        if not _CONNECTOR_RE.match(ref) or comp.get("dnp") or not re.search(
                "USB", (comp.get("value") or "") + " "
                + (comp.get("libsource") or ("", ""))[1], re.I):
            continue
        pins = comp_pins.get(ref, {})
        funcs = {p: _norm_pin(_pin_func(nets, ref, p, n))
                 for p, n in pins.items()}
        vb = sorted({pins[p] for p, f in funcs.items()
                     if re.match(r"^VBUS", f, re.I)
                     and not pins[p].startswith("unconnected-")})
        if not vb:
            continue
        checked += 1
        ss = any(_SL_SS_RE.match(f) for f in funcs.values())
        typec = any(_SL_CC_RE.match(f) for f in funcs.values())
        lim = float(usb.get("usb3_a" if ss else "usb2_a",
                            0.9 if ss else 0.5))
        ld = _sl_rail_load(vb[0], nets, comps, comp_pins, table, rails,
                           {ref})
        if ld["peak"] > lim + 1e-9:
            more = (" A Type-C source may offer 1.5 A or 3 A on CC without "
                    "PD, so this holds only on a default-current port"
                    if typec else "")
            yield ("vbus_over", "warning" if typec else "error", vb[0],
                   [ref], (
                       f"'{vb[0]}' from {ref} feeds loads that ask "
                       f"{ld['peak'] * 1000:.3g} mA "
                       f"({', '.join(ld['known'])}), past the "
                       f"{lim * 1000:g} mA a USB {'3' if ss else '2'} port "
                       f"gives with no power-delivery controller on the "
                       f"board.{more} [USB 2.0 7.2.1, USB 3.2 11.4.1; "
                       f"{_sl_src(ld['rows'])}]"),
                   ld["rows"], {"limit_a": lim,
                                "load_a": round(ld["peak"], 4)})
    yield ("checked", checked)


def _pair_rules(v, not_judged, violations, nets, comps, comp_pins, rails,
                stated):
    """Report lib/pair_order.py's findings; connector pairs checked."""
    import pair_order as po  # noqa: PLC0415
    h = {"part_kind": _part_kind, "is_ground": _is_ground_name}
    swapped_nets = {x.get("net") for x in violations
                    if x.get("kind") == "pin_net_swap"}
    why = {"usb": "USB 2.0 receivers take the pair as wired: the host "
                  "reads J for K and the device never enumerates "
                  "[USB 2.0 spec 7.1.7]",
           "lvds": "an LVDS receiver has no polarity correction, so every "
                   "bit of that lane arrives inverted",
           "eth": "most PHYs correct a reversed pair (10BASE-T and "
                  "1000BASE-T autopolarity; 100BASE-TX's MLT-3 doesn't "
                  "care), but check this PHY's datasheet before relying "
                  "on it",
           "mdi": "1000BASE-T PHYs correct a reversed pair, but check "
                  "this PHY's datasheet before relying on it",
           "e1": "AMI is polarity-free, but a framer counting bipolar "
                 "violations or a B8ZS/HDB3 substitution reads them "
                 "inverted on a crossed TIP/RING",
           "lane": "some bridges can swap a lane's polarity in a "
                   "register; without that the lane reads inverted"}
    checked = 0
    for hit in po.pair_order(nets, comps, comp_pins, rails, h, stated):
        if "checked" in hit:
            checked = hit["checked"]
            continue
        if "not_judged" in hit:
            not_judged.append(hit["not_judged"])
            continue
        if swapped_nets & set(hit["walked"]):
            continue  # rule 8 already names the crossed pin
        c, f, lab = hit["conn"], hit["far"], hit["label"]
        fv = comps.get(f, {}).get("value")
        fp, fn_ = hit["far_funcs"], hit["far_pins"]
        refs = [c, f]
        if hit["rule"] == "pair_polarity_swap":
            v(hit["sev"], "pair_polarity_swap", hit["p_net"], refs,
              f"{c}'s {lab} (named by {hit['how']}) meets {f} ({fv}) the "
              f"other way round: {c} pin {hit['p_pin']} (+) on "
              f"'{hit['p_net']}' reaches {f} {fp['P']} (pin {fn_['P']}), "
              f"and pin {hit['n_pin']} (-) on '{hit['n_net']}' reaches "
              f"{fp['N']} (pin {fn_['N']}). {why[hit['family']]}. Swap "
              f"the two lines at one end",
              connector=c, part=f, family=hit["family"], nets=hit["nets"])
        elif hit["rule"] == "pair_split":
            v("error", "pair_split", hit["p_net"], refs,
              f"{c}'s {lab} (named by {hit['how']}) is split at {f} "
              f"({fv}): its + line '{hit['p_net']}' reaches {fp['P']} "
              f"(pin {fn_['P']}) and its - line '{hit['n_net']}' reaches "
              f"{fp['N']} (pin {fn_['N']}), halves of two different "
              f"pairs. Neither pair is a pair any more: the twist and the "
              f"far end's termination no longer carry one signal. Wire "
              f"both lines of the pair to one pair of {f}",
              connector=c, part=f, family=hit["family"], nets=hit["nets"])
        elif hit["rule"] == "pair_lane_order":
            v("error", "pair_lane_order", hit["p_net"], refs,
              f"{c}'s {lab} (named by {hit['how']}) reaches {f} ({fv})'s "
              f"{hit['far_key']} pair ({fp['P']}/{fp['N']}): the lanes "
              f"are out of order, and an LVDS serializer sends each bit "
              f"on a fixed lane, so the panel reads the wrong bits. Put "
              f"lane {hit['key']} on {f}'s {hit['key']} pins",
              connector=c, part=f, family=hit["family"], nets=hit["nets"])
    return checked


def _signal_rules(v, vt, not_judged, nets, comps, comp_pins, rails,
                  io_table, abs_table, pl_specs, io_tolerance):
    """Report lib/signal_domains.py's findings; (polarity pins, I/O pairs)
    checked."""
    import signal_domains as sd  # noqa: PLC0415
    tol = sd.load_io_tolerance() if io_tolerance is None else io_tolerance
    try:
        import yaml  # noqa: PLC0415
        io_sup = (yaml.safe_load(IO_SUPPLY.read_text(encoding="utf-8"))
                  or {}).get("parts") or []
    except (OSError, ImportError):
        io_sup = []
    h = {"norm_pin": _norm_pin, "part_kind": _part_kind,
         "parse_value": engcalc.parse_value, "is_ground": _is_ground_name,
         "base_type": _base_type, "spec_matches": _spec_matches,
         "abs_row": _abs_row, "leak_row": _leak_row}

    def desc(p):
        return f"{p['ref']} {p['func']} (pin {p['pin']}, asserts {p['level']})"

    pol = 0
    for hit in sd.polarity(nets, comps, comp_pins, rails, h):
        if "checked" in hit:
            pol = hit["checked"]
            continue
        if hit["rule"] == "polarity_mismatch":
            ps = hit["lows"] + hit["highs"]
            refs = sorted({p["ref"] for p in ps})
            via = (f", joined through {', '.join(hit['joins'])}"
                   if hit["joins"] else "")
            net = hit["nets"][0]
            if hit["cross"]:
                o = hit["lows"][0]
                v("warning", "polarity_mismatch", net, refs,
                  f"{desc(o)} drives {'; '.join(desc(p) for p in hit['highs'])}"
                  f" on '{'/'.join(hit['nets'])}'{via}: the output's 'run' "
                  f"level is the input's 'stop' level, so the part is held "
                  f"off while the source says all is well, and runs while "
                  f"it is held in reset or fault. Check both datasheets' "
                  f"pin tables; an inverter or the other-polarity part "
                  f"fixes it", nets=hit["nets"], kind_of=hit["kind"])
                continue
            sev = "warning" if hit["kind"] == "irq" else "error"
            extra = (" Some interrupt outputs are programmable; check the "
                     "firmware sets the level the input expects."
                     if hit["kind"] == "irq" else "")
            v(sev, "polarity_mismatch", net, refs,
              f"{hit['kind']} pins of opposite active level meet on "
              f"'{'/'.join(hit['nets'])}'{via}: "
              f"{'; '.join(desc(p) for p in hit['lows'])} against "
              f"{'; '.join(desc(p) for p in hit['highs'])}. When one side "
              f"asserts the other is released, so the line holds a part in "
              f"its asserted state at idle.{extra} Their names say the "
              f"levels; check the datasheets' pin tables",
              nets=hit["nets"], kind_of=hit["kind"])
        elif hit["rule"] == "polarity_net_name":
            ps = hit["pins"]
            v("warning", "polarity_net_name", hit["net"],
              sorted({p["ref"] for p in ps}),
              f"'{hit['net']}' is named as a {hit['kind']} asserting "
              f"{hit['level']}, but every {hit['kind']} pin on it asserts "
              f"the other way: {'; '.join(desc(p) for p in ps)}. Either "
              f"the name or the part is wrong; firmware or the next "
              f"designer reading the name will get the level backwards",
              kind_of=hit["kind"])
        elif hit["rule"] == "idle_pull_asserted":
            m = hit["pin"]
            pulls = ", ".join(f"{r} to {n}" for r, n in hit["pulls"])
            od = (f" (the open-drain {', '.join(x['ref'] for x in hit['ods'])}"
                  f" can only pull it low too)" if hit["ods"] else "")
            v("error", "idle_pull_asserted", hit["net"], [m["ref"]],
              f"{m['ref']} {m.get('pinfunction')} (pin {m['pin']}) is a "
              f"{hit['kind']} input asserting {hit['level']}, and nothing "
              f"on '{hit['net']}' drives it: its only pull ({pulls}) holds "
              f"it {hit['level']}{od}, so it stays asserted for good. Pull "
              f"it to the other side", kind_of=hit["kind"])
    tables = {"io_levels": io_table, "pin_abs_max": abs_table,
              "pin_leakage": pl_specs, "io_tolerance": tol,
              "io_supply": io_sup}
    vt_tables = {"io_levels": io_table,
                 "pin_abs_max": _verified_only(abs_table),
                 "pin_leakage": _verified_only(pl_specs),
                 "io_tolerance": _verified_only(tol), "io_supply": io_sup}
    ok = {(x["rule"], x.get("driver"), x["receiver"], x.get("net"))
          for x in sd.domains(nets, comps, comp_pins, rails, h, vt_tables)
          if "checked" not in x}
    dom = 0
    for hit in sd.domains(nets, comps, comp_pins, rails, h, tables):
        if "checked" in hit:
            dom = hit["checked"]
            continue
        src = ""
        if hit.get("row"):
            src = " [" + " ".join(str(hit["row"].get(
                "source") or hit["row"].get("mpn") or "").split()) + "]"
        if hit["rule"] == "io_domain_pull_rail":
            r = hit["receiver"]
            rv = f" on its {hit['vr']:g} V rail" if hit["vr"] else ""
            if hit["how"] == "unrated":
                not_judged.append(
                    f"io_domain_pull_rail on {hit['net']}: {hit['pull']} "
                    f"pulls it to {hit['rail']} ({hit['vp']:g} V), above "
                    f"{r} ({comps[r].get('value')}){rv}; no datasheet row "
                    f"says whether {hit['rfunc']} is {hit['vp']:g} V "
                    f"tolerant")
                continue
            over = hit["how"] == "over"
            key = (hit["rule"], None, r, hit["net"])
            vt(key in ok, "error" if over else "warning",
               "io_domain_pull_rail", hit["net"], [hit["pull"], r],
               f"open-drain line '{hit['net']}' is pulled by {hit['pull']} "
               f"to {hit['rail']} ({hit['vp']:g} V), "
               + (f"above the {hit['limit']:.2f} V {r} "
                  f"({comps[r].get('value')}) {hit['rfunc']}{rv} takes"
                  if over else
                  f"below the {hit['limit']:.2f} V VIH of {r} "
                  f"({comps[r].get('value')}) {hit['rfunc']}{rv}, so it "
                  f"may never read high")
               + f". Pull it to the receiver's rail, or use a translator"
               f"{src}", pull_rail=hit["rail"], receiver=r)
            continue
        d, r = hit["driver"], hit["receiver"]
        nets_s = ", ".join(f"'{n}'" for n in hit["nets"][:6]) + (
            f" and {len(hit['nets']) - 6} more" if len(hit["nets"]) > 6
            else "")
        pins_s = ", ".join(f"{a}->{b}" for a, b in hit["pins"][:6])
        ser = (f" (through {', '.join(hit['series'])})" if hit["series"]
               else "")
        rv = f" on a {hit['vr']:g} V rail" if hit["vr"] else ""
        head = (f"{d} ({comps[d].get('value')}) drives {r} "
                f"({comps[r].get('value')}){rv} at {hit['vd']:g} V "
                f"({hit['dwhy']}) on {nets_s}{ser} [{pins_s}]")
        if hit["rule"] == "unrated":
            not_judged.append(
                f"io_domain_overdrive: {head}; no datasheet row says "
                f"whether {r}'s inputs are {hit['vd']:g} V tolerant")
            continue
        key = (hit["rule"], d, r, None)
        if hit["rule"] == "io_domain_overdrive":
            vt(key in ok, "error", "io_domain_overdrive", hit["nets"][0],
               [d, r],
               f"{head}: above the {hit['limit']:.2f} V its inputs take. "
               f"The input's clamp diodes carry the difference into its "
               f"rail. A level translator, a part tolerant of "
               f"{hit['vd']:g} V, or both parts on one rail fixes it{src}",
               nets=hit["nets"], driver_v=hit["vd"], limit_v=hit["limit"])
        else:
            vt(key in ok, "warning", "io_domain_underdrive", hit["nets"][0],
               [d, r],
               f"{head}: its high is below the {hit['limit']:.2f} V VIH "
               f"{r} needs, so a high may read low or as noise. An input "
               f"of the HCT/AHCT kind, or a translator, fixes it{src}",
               nets=hit["nets"], driver_v=hit["vd"], vih_v=hit["limit"])
    return pol, dom


def audit(parsed: dict, constraints: dict,
          decoupling: dict | None,
          pin_requirements: list[dict] | None = None,
          board_only_refs: set[str] | None = None,
          pin_states: list[dict] | None = None,
          mcu_pins: dict | None = None,
          i2c_addresses: list[dict] | None = None,
          pin_leakage: list[dict] | None = None,
          io_levels: dict | None = None,
          bus_load: dict | None = None,
          stock_symbols: Path | str | None = None,
          pin_abs_max: dict | None = None,
          part_pinouts: list[dict] | None = None,
          source_limits: dict | None = None,
          io_tolerance: list[dict] | None = None
          ) -> tuple[list[dict], dict]:
    """stock_symbols: KiCad's stock symbol dir for the pin-map rule;
    None looks it up (lib/env.kicad_symbol_dir), "" turns the rule off.
    part_pinouts: the datasheet pin tables the rule falls back on
    (reference/part_pinouts.yaml when None)."""
    nets = parsed["nets"]
    comps = parsed["components"]
    violations: list[dict] = []

    def v(sev, kind, net, refs, msg, **extra):
        violations.append(checklib.violation(
            SCRIPT, sev, None, None, net, refs, msg, SOURCE,
            kind=kind, **extra))

    # A datasheet-table finding that only an unverified row makes (rules
    # 15-19, #1100) is reported as not judged, not as a fault.
    not_judged: list[str] = []

    def vt(judged, sev, kind, net, refs, msg, **extra):
        if judged:
            v(sev, kind, net, refs, msg, **extra)
        else:
            not_judged.append(f"{kind} ({sev}) on {net or ', '.join(refs)}: "
                              f"{msg} -- rests on a drafted row not yet "
                              f"verified against its datasheet")

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
    # A row of a part without verified: true (a part_rows.py draft) gives
    # not_judged only (#1100).
    pr_ok = {(h[2], h[3]["id"], h[3]["pin"], h[3]["to"]) for h in
             _pin_requirements(nets, comps, comp_pins,
                               _verified_only(specs))}
    for sev, net, ic, req, found, elsewhere, off, unread in _pin_requirements(
            nets, comps, comp_pins, specs):
        judged = (ic, req["id"], req["pin"], req["to"]) in pr_ok
        want = req.get("value") or "/".join(req["kinds"])
        what = (f"{ic} ({comps[ic].get('value')}) needs {req['id']} "
                f"({want}) from {req['pin']} to {req['to']} "
                f"[{' '.join(str(req.get('source', 'datasheet')).split())}]")
        if found is None:  # a `distinct` pair tied into one net
            vt(judged, "error", "pin_part_missing", net, [ic],
               f"{what}: {req['pin']} is tied straight to {req['to']} on "
               f"'{net}', so there is no {req['id']} between them",
               part=req["id"], pin=req["pin"], to=req["to"], tied=True,
               off_value=[])
        elif unread and not elsewhere:
            # a part spans the pins but its value can't be read: not judged,
            # never reported off-value (#1160 false finding: "0805,C0.1")
            names = ", ".join(f"{u} ({comps[u].get('value')!r})"
                              for u in unread)
            not_judged.append(
                f"pin_part_missing ({sev}) on {net or ic}: {what}: "
                f"{names} spans those pins, but its value can't be read")
        elif not elsewhere:
            why = "no part spans those pins" + (
                f"; off-value parts across it: {', '.join(off)}"
                if off else "")
            if req.get("_sum") is not None:
                why = (f"{' + '.join(req['_sum_of'])} across those pins "
                       f"together give {req['_sum'] * 1e6:.3g} uF, below "
                       f"its {float(req['min']) * 1e6:.3g} uF minimum"
                       + (" (nominal; mlcc_dcbias judges it derated)"
                          if req.get("effective") else ""))
            vt(judged, sev, "pin_part_missing", net, [ic],
              f"{what}: {why}",
              part=req["id"], pin=req["pin"], to=req["to"],
              off_value=off)
        else:
            vt(judged, "warning", "pin_part_missing", net, [ic],
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
    ps_ok = {(h[2], h[3], h[8]) for h in _pin_states(
        nets, comps, comp_pins, ps_rails, _verified_only(ps_specs))}
    for (rule, spec, ref, pnum, name, st, volts, why,
         problem) in _pin_states(nets, comps, comp_pins, ps_rails, ps_specs):
        via = f" (via {', '.join(why)})" if why else ""
        vt((ref, pnum, problem) in ps_ok,
           rule.get("severity", "error"), "pin_state", name, [ref],
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
    pwm_ok = {(h[0]["mpn"], h[1]) for h in _mcu_pwm_groups(
        nets, comps, comp_pins, ps_rails, _verified_only(mp_table))[0]}
    for row, mref, mrow, inputs in pwm_found:
        pins = "; ".join(
            f"{lr} {lp} on {mp} ({'/'.join(fs) if fs else 'no timer'})"
            for lr, lp, mp, fs in inputs)
        vt((row["mpn"], mref) in pwm_ok,
           row.get("severity", "warning"), "mcu_pwm_timer", None,
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
    pair_found, pair_checked = _mcu_pwm_pairs(nets, comps, comp_pins,
                                              ps_rails, mp_table)
    pair_ok = {(h[1], h[2]) for h in _mcu_pwm_pairs(
        nets, comps, comp_pins, ps_rails, _verified_only(mp_table))[0]}
    for row, lref, mref, mrow, legs in pair_found:
        pins = "; ".join(
            f"{hi}/{lo} on {hp}/{lp} "
            f"({'/'.join(hf) if hf else 'no timer'} | "
            f"{'/'.join(lf) if lf else 'no timer'})"
            for hi, lo, hp, lp, hf, lf in legs)
        vt((lref, mref) in pair_ok,
           row.get("severity", "warning"), "mcu_pwm_pair", None,
           [lref, mref],
           f"{lref} ({comps[lref].get('value')}) takes its high-side/"
           f"low-side inputs from {mref} ({comps[mref].get('value')}) "
           f"pins that no single timer gives each leg one channel's CHx "
           f"and CHxN: {pins}. "
           f"{' '.join(row['why'].split())} "
           f"[{' '.join(row['source'].split()).split(',')[0]}; "
           f"{' '.join(mrow['source'].split()).split('(')[0].strip()}]"
           + ("" if row.get("verified") and mrow.get("verified")
              else " (not yet re-read)"),
           mcu=mref, part=row["mpn"],
           legs=[{"high": hi, "low": lo, "mcu_high": hp, "mcu_low": lp,
                  "timers_high": hf, "timers_low": lf}
                 for hi, lo, hp, lp, hf, lf in legs])
    io_checked = 0
    for row in mp_table.get("mcus") or []:
        if row.get("input_only"):
            io_checked += sum(1 for r in comps
                              if _mcu_row(comps[r], mp_table.get("mcus")
                                          or []) is row)
    io_ok = {(h[1], h[2], h[4]) for h in _input_only_selects(
        nets, comps, comp_pins, _verified_only(mp_table))}
    for (mrow, mref, mpin, mnet, sref, spin,
         snet) in _input_only_selects(nets, comps, comp_pins, mp_table):
        via = "" if snet == mnet else f" over a 0 ohm link from '{mnet}'"
        vt((mref, mpin, sref) in io_ok,
           "warning", "mcu_input_only_drive", snet, [mref, sref],
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
    i2c_ok = set()
    for scl, devs in _i2c_buses(nets, comps, comp_pins, ps_rails,
                                _verified_only(i2c_rows)).items():
        seen: dict[int, int] = {}
        for d in devs:
            seen[d[0]] = seen.get(d[0], 0) + 1
        i2c_ok |= {(scl, a) for a, n in seen.items() if n > 1}
    for scl, devs in sorted(buses.items()):
        by_adr: dict[int, list[tuple]] = {}
        for d in devs:
            by_adr.setdefault(d[0], []).append(d)
        for adr, same in sorted(by_adr.items()):
            if len(same) < 2:
                continue
            who = ", ".join(f"{r} ({comps[r].get('value')}, "
                            + (f"{row['pin']} to {where})" if "pin" in row
                               else "fixed address)")
                            for _, r, row, where in same)
            vt((scl, adr) in i2c_ok, "error", "i2c_address_collision", scl,
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
    pull_ok = {(h["net"], h["severity"]) for h in _pull_vs_leakage(
        nets, comps, comp_pins, pl_rails, _verified_only(pl_specs))}
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
        vt((hit["net"], hit["severity"]) in pull_ok,
           hit["severity"], "pull_vs_leakage", hit["net"], refs,
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
    cont_ok = {(h[0], h[1]) for h in _driver_contention(
        nets, comps, comp_pins, pl_rails, _verified_only(pl_specs))}
    for name, rpin, drivers, spec in _driver_contention(
            nets, comps, comp_pins, pl_rails, pl_specs):
        contention_checked += 1
        vt((name, rpin) in cont_ok, "warning", "driver_contention", name,
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
    iolv_ok = {(h["net"], h["driver"], h["receiver"])
               for h in _io_overvoltage(nets, comps, comp_pins, pl_rails,
                                        _verified_only(io_table))
               if "checked" not in h and h["over"]}
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
        vt((hit["net"], hit["driver"], hit["receiver"]) in iolv_ok,
           "error", "io_overvoltage", hit["net"],
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

    # 21. more capacitance on a bus line than the bus allows
    # (reference/bus_load.yaml, #1031; errata corpus: echoplayer r1-rev1
    # put a CESD3V3AP clamp, 120 pF, on every SDMMC line)
    bl = load_bus_load() if bus_load is None else bus_load
    budgets, bl_parts = bl.get("buses") or {}, bl.get("parts") or []
    lines = _bus_lines(nets, comps, comp_pins)
    for net, (bus, skip) in sorted(lines.items()):
        b = budgets.get(bus)
        if not b:
            continue
        total, who = _line_load(net, skip, nets, comps, comp_pins,
                                ps_rails, bl_parts)
        budget, top = float(b["budget_pf"]), b.get("max_pf")
        if total <= budget:
            continue
        sev = "error" if bus == "sd" or top is None or total > float(top) \
            else "warning"
        parts = ", ".join(
            f"{r} ({comps[r].get('value')}) {pf:g} pF" for r, pf, _ in who)
        srcs = [b] + [row for _, _, row in who if row]
        cite = "; ".join(dict.fromkeys(
            " ".join(s["source"].split()).split(":")[0].split(" (")[0]
            for s in srcs))
        v(sev, "bus_cap_load", net, sorted(r for r, _, _ in who),
          f"{bus.upper()} line '{net}' carries at least {total:g} pF from "
          f"{parts}, over the {budget:g} pF {bus.upper()} budget"
          + (f" ({b['what']})" if b.get("what") else "")
          + (f" (and over {float(top):g} pF)" if top and total > float(top)
             else "")
          + f"; the host, the card and the copper add more [{cite}]"
          + ("" if all(s.get("verified") for s in srcs)
             else " (not yet re-read)"),
          bus=bus, load_pf=round(total, 3), budget_pf=budget)

    # 22. power-path orientation (#1085; TI SLVA139: "the FET's body
    # diode is oriented in the direction of normal current flow"; ABLIC
    # S-8261 Rev.5.5_00 Figure 10: VSS on the cell's -, VM through R2 to
    # the pack's - EB-)
    pp_checked = 0
    for ref, ch, bat, other in _power_path_fets(nets, comps, comp_pins,
                                                pl_rails):
        if ref == "checked":
            pp_checked = ch
            continue
        d_s = "source to drain" if ch == "N" else "drain to source"
        v("warning", "power_path_body_diode", bat, [ref],
          f"{ref} ({comps.get(ref, {}).get('value')}, {ch}-channel) has its "
          f"body diode ({d_s}) from '{other}' into the battery net '{bat}': "
          f"with the FET off, '{other}' still charges the battery through "
          f"the diode, uncontrolled. Turn the FET around so the diode points "
          f"out of the battery [TI SLVA139; Microchip AN1149 Figs 5-6]",
          from_net=other)
    # a load switch or reverse-polarity FET off the battery nets (#1085;
    # TI SLVA139 Fig 3: a P-FET's drain on the input, its gate to ground;
    # a high-side P-FET's source on the supply, a low-side N-FET's on
    # ground, or the body diode carries the load with the FET off)
    ps_checked, sw_nj = 0, {}
    for ref, ch, case, sup, other in _power_switches(nets, comps, comp_pins,
                                                     pl_rails):
        if ref == "checked":
            ps_checked, sw_nj = ch, case
            continue
        d_s = "source to drain" if ch == "N" else "drain to source"
        val = comps.get(ref, {}).get("value")
        if case == "low":
            msg = (f"{ref} ({val}, N-channel) switches the low side with its "
                   f"drain on ground '{sup}' and its source on '{other}': its "
                   f"body diode ({d_s}) carries the load's current to ground "
                   f"with the FET off. Put the source on ground and the "
                   f"drain on the load")
        elif case == "reverse":
            msg = (f"{ref} ({val}, P-channel, gate to ground) has its source "
                   f"on the input '{sup}' and its drain on '{other}': with "
                   f"the input reversed its body diode ({d_s}) passes the "
                   f"reversed voltage to the load. The drain goes on the "
                   f"input, the source on the load [TI SLVA139 Fig 3]")
        else:
            msg = (f"{ref} ({val}, {ch}-channel) switches '{other}' from "
                   f"'{sup}' with its body diode ({d_s}) pointing from "
                   f"'{sup}' into the load: with the FET off, the load "
                   f"still runs through the diode. Turn the FET around so "
                   f"the {'source' if ch == 'P' else 'drain'} is on '{sup}'"
                   f" [TI SLVA139]")
        v("warning", "power_switch_reversed", sup, [ref], msg,
          from_net=other, case=case)
    prot_checked = 0
    for ref, vss, pack, on_vss, on_pack in _protector_orientation(
            nets, comps, comp_pins):
        if ref == "checked":
            prot_checked = vss
            continue
        v("warning", "protector_orientation", vss, [ref] + on_vss,
          f"{ref} ({comps.get(ref, {}).get('value')}) VSS net '{vss}' "
          f"carries the ground of {', '.join(on_vss)}, while the pack "
          f"negative '{pack}' its VM resistor reaches carries "
          f"{', '.join(on_pack) or 'none'}: VSS belongs on the cell's - "
          f"alone and the load returns through the FETs to the pack - "
          f"(EB-), or the protector cannot cut it [ABLIC S-8261 Rev.5.5_00 "
          f"Figure 10]", pack_net=pack)

    # 23. an off-board input held only by a weak pull, with no clamp on the
    # connector pin (#1079; errata corpus: pdlc v1's EN terminal, 100k to
    # +12V into a PNP's base, was "much too sensitive when left floating";
    # v2 has 22k to +3.3V and an ESD array on it)
    ob_checked = 0
    for hit in _offboard_weak_pull(nets, comps, comp_pins, pl_rails):
        if "checked" in hit:
            ob_checked = hit["checked"]
            continue
        ohm, fv, pref, far = hit["pull"]
        who = ", ".join(r + (f" (through {via})" if via else "")
                        for r, via in hit["readers"])
        v("warning", "offboard_input_weak_pull", hit["net"],
          sorted({c.split(".")[0] for c in hit["connectors"]} | {pref}
                 | {r.split(".")[0] for r, _ in hit["readers"]}
                 | {via for _, via in hit["readers"] if via}),
          f"'{hit['net']}' leaves the board at "
          f"{', '.join(hit['connectors'])} and is held only by {pref} "
          f"{comps[pref].get('value')} to {far}, with no TVS or ESD clamp "
          f"on it, into {who}: with the cable out or on a long wire it picks "
          f"up enough to switch. Use a pull of {_OFFBOARD_PULL_MAX / 1e3:g}k "
          f"or less and clamp the pin [TI SPRACK9 App. A Table 2: 4.7K to "
          f"10K on a line that may be disconnected; Microchip AN2587 PCB "
          f"rule 8: TVS on all external connector I/O pins]",
          pull_ohm=ohm, pull_to=far, connectors=hit["connectors"])

    # 24. a symbol whose name gives a size its part's Value or footprint
    # contradicts: another part's symbol placed (#1086; errata corpus:
    # netscore-big's 0.8in digits drawn with a '7Seg 0.2in' symbol)
    sz_checked = 0
    for ref in sorted(comps):
        sp = _symbol_part(ref, comps[ref])
        if not sp or len(comp_pins.get(ref, {})) <= 2 \
                or not pinmaplib.dimensions(sp[1]):
            continue
        sz_checked += 1
        hit = pinmaplib.size_conflict(sp[1], comps[ref].get("value"),
                                      comps[ref].get("footprint"))
        if hit:
            mine, theirs, where = hit
            v("warning", "symbol_value_mismatch", None, [ref],
              f"{ref} is drawn with symbol {sp[0]}:{sp[1]} "
              f"({pinmaplib.fmt_dims(mine)}) but its {where} says "
              f"{pinmaplib.fmt_dims(theirs)} ('{comps[ref].get('value')}', "
              f"{comps[ref].get('footprint')}): another part's symbol, "
              f"whose pin map need not be this part's. Check every pin "
              f"against the placed part's datasheet",
              symbol=f"{sp[0]}:{sp[1]}", sizes=pinmaplib.fmt_dims(theirs))

    # 25. a symbol whose pin map disagrees with KiCad's stock symbol for
    # the part its Value or MPN names, in the same package (#1086 class 5;
    # a regulator's IN and OUT swapped between package variants)
    symdir = stock_symbols
    if symdir is None:
        from lib import env  # noqa: PLC0415
        symdir = env.kicad_symbol_dir()
    pp_rows = load_part_pinouts() if part_pinouts is None else part_pinouts
    pm_hits, pm_checked, pm_by_table = _symbol_pinmap_hits(
        parsed, symdir, pp_rows) if symdir else ([], 0, 0)
    for ref, (lib, part), (slib, sname, st), diffs, row in pm_hits:
        theirs = "the stock symbol" if row is None else "the datasheet"
        said = "; ".join(f"{n} on {'/'.join(a)} here, {'/'.join(b)} in "
                         f"{theirs}" for n, a, b in diffs[:6])
        if row is None:
            vs = (f"KiCad's stock {slib}:{sname} "
                  f"({st['footprint'] or 'no default footprint'})")
            extra = {"stock": f"{slib}:{sname}"}
        else:
            vs = (f"the {sname} pin table "
                  f"({row.get('source') or 'reference/part_pinouts.yaml'})")
            extra = {"pinout": sname}
        vt(row is None or row.get("verified") is True,
           "warning", "symbol_pinmap", None, [ref],
           f"{ref} ('{comps[ref].get('value')}', "
           f"{comps[ref].get('footprint')}) is drawn with {lib}:{part}, "
           f"whose pins disagree with {vs}: {said}. A pin "
           f"map from another package variant or another part puts these "
           f"signals on the wrong pads; check them against the datasheet's "
           f"pin table for this package",
           symbol=f"{lib}:{part}",
           pins=[{"name": n, "placed": a, "stock": b} for n, a, b in diffs],
           **extra)

    # 26. sibling channels of one part whose nets count opposite ways: one
    # unit's pins drawn in reverse (errata corpus: gcm v5.0's SN74LV393A,
    # 1QA..1QD on d16..d2 while 2QA..2QD run d32..d256)
    ch_checked = 0
    for ref in sorted(comps):
        if not _symbol_part(ref, comps[ref]):
            continue
        pins = {}
        for pnum, net in comp_pins.get(ref, {}).items():
            func = next((m.get("pinfunction") for m in nets.get(net, [])
                         if m["ref"] == ref and m["pin"] == pnum), "")
            pins[pnum] = (func, net)
        flips = pinmaplib.flipped_channels(pins)
        if any(pinmaplib._channel(f) for f, _ in pins.values()):
            ch_checked += 1
        for fl in flips:
            said = "; ".join(
                f"unit {u} {' '.join(p[0] for p in d['pins'])} on "
                f"{', '.join(p[2] for p in d['pins'])} ({d['direction']})"
                for u, d in fl["units"].items())
            v("warning", "bus_order_flipped", None, [ref],
              f"{ref} ({comps[ref].get('value')}) has channels of one kind "
              f"whose nets count opposite ways: {said}. One unit's outputs "
              f"wired in reverse, as a symbol drawn with that unit's pins "
              f"out of order does; check the order against the datasheet",
              stem=fl["stem"])

    # 27. an MCU I2C block with its SDA and SCL pins on the other's net
    # (mcu_pins.yaml i2c; errata corpus: pico-if1 v1.3 put the OLED's SCK
    # on GPIO4, I2C0 SDA, and its SDA on GPIO5, I2C0 SCL)
    i2c_found, i2c_checked = _mcu_i2c_swaps(nets, comps, comp_pins,
                                            mp_table)
    i2c_ok = {(h[1], h[2]) for h in _mcu_i2c_swaps(
        nets, comps, comp_pins, _verified_only(mp_table))[0]}
    for row, ref, block, lines in i2c_found:
        said = "; ".join(f"{func} (pin {pnum}, {f}) on '{net}', a {role} "
                         f"line" for pnum, func, f, net, role in lines)
        vt((ref, block) in i2c_ok, "warning", "mcu_i2c_role", None,
           [ref],
           f"{ref} ({comps[ref].get('value')}) meets its I2C bus with "
           f"{block}'s lines crossed: {said}. The hardware {block} block "
           f"cannot drive this bus unless firmware bit-bangs/PIO "
           f"[{' '.join(row['source'].split()).split(',')[0]}]",
           mcu=ref, block=block, part=row["mpn"])

    # 28. every net that leaves the board, against the receiving pin's
    # absolute maximum ratings both ways, its level and its clamps
    # (reference/pin_abs_max.yaml; class 3 of docs/miss-taxonomy.md;
    # errata corpus: zxdiag v1.1's 1N4148 clamps let -1 V reach the Pico
    # ADC from a divider topped by the system under test's +5V, smart-lcc
    # rev A ran its TX straight out a cable, fc980c rev1's KEY_STATE had
    # no pull)
    abs_table = load_pin_abs_max() if pin_abs_max is None else pin_abs_max
    obi_ok, obi_judged = set(), 0
    for h in _offboard_inputs(nets, comps, comp_pins, pl_rails,
                              _verified_only(abs_table)):
        if "checked" in h:
            obi_judged = h["checked"]
        elif h["kind"] != "float":
            obi_ok.add((h["net"], h["receiver"], h["kind"]))
    for hit in _offboard_inputs(nets, comps, comp_pins, pl_rails, abs_table):
        if "checked" in hit:
            continue
        kind = hit["kind"]
        if kind == "float" and hit.get("own_rail"):
            v("info", "offboard_input_floating", hit["net"],
              sorted({c.split(".")[0] for c in hit["connectors"]}
                     | {r.split(".")[0] for r in hit["readers"]}),
              f"'{hit['net']}' comes in at {', '.join(hit['connectors'])} "
              f"and goes to {', '.join(hit['readers'])} with no pull on "
              f"this board; its cable brings {', '.join(hit['hot'])}, but "
              f"the reader's pins are rated below {hit['top']:g} V, so the "
              f"line can only idle at the reader's own rail: pull set in "
              f"gateware or firmware, not on the board",
              connectors=hit["connectors"], pull="configurable")
            continue
        if kind == "float":
            v("warning", "offboard_input_floating", hit["net"],
              sorted({c.split(".")[0] for c in hit["connectors"]}
                     | {r.split(".")[0] for r in hit["readers"]}),
              f"'{hit['net']}' comes in at {', '.join(hit['connectors'])} "
              f"and goes to {', '.join(hit['readers'])} with no pull "
              f"resistor, no driver and no clamp on this board, on a cable "
              f"that also brings {', '.join(hit['hot'])} ("
              + (f"the reader runs at {hit['rvdd']:g} V" if hit["rvdd"]
                 is not None else "the reader's supply net carries no "
                 "voltage; taken as 3.3 V")
              + "): a status line whose far end is open "
              f"collector at its own level, or unplugged, leaves the input "
              f"floating, and a pull inside the reader isn't at that level. "
              f"Pull it to the level it idles at "
              f"[TI SPRACK9 App. A Table 2: 4.7K to 10K on a line that may "
              f"be disconnected]",
              connectors=hit["connectors"])
            continue
        row = hit["row"]
        rc = comps[hit["receiver"]]
        pin = f"{hit['receiver']} ({rc.get('value')}) {hit['rfunc']}"
        lim = (f"{hit['vmin']:g} V to {hit['vmax']:g} V" if hit["vmin"]
               is not None and hit["vmax"] is not None
               else f"at most {hit['vmax']:g} V" if hit["vmax"] is not None
               else f"at least {hit['vmin']:g} V")
        src = " ".join(row["source"].split())
        where = (f"at {', '.join(hit['connectors'])}" if hit["connectors"]
                 else "through " + ", ".join(f"{r} from {far}"
                                             for r, far in hit["via"]))
        refs = sorted({hit["receiver"]}
                      | {c.split(".")[0] for c in hit["connectors"]}
                      | {r for r, _ in hit["via"]})
        judged = (hit["net"], hit["receiver"], kind) in obi_ok
        if kind == "bare":
            vt(judged, "warning", "offboard_input_unprotected", hit["net"],
               refs,
               f"'{hit['net']}' runs from the cable connector "
               f"{', '.join(hit['cables'])} straight to {pin}, which takes "
               f"only {hit['margin']:g} V past its rail ({lim}), with no "
               f"series resistor and no clamp, on a cable that also carries "
               f"{', '.join(hit['hot'])}: a miswire, a hot plug or "
               f"ESD on the cable lands on the pin. Put a series resistor "
               f"and a clamp (TVS, zener or Schottky) between the connector "
               f"and the pin [{src}; Microchip AN2587 PCB rule 8: TVS on "
               f"all external connector I/O pins]",
               margin_v=hit["margin"], connectors=hit["cables"])
            continue
        why = ("with every rail present" if hit["absent"] is None
               else f"with the off-board rail '{hit['absent']}' absent "
                    f"(nothing on this board powers it)")
        c = hit.get("clamp")
        ctext = (f"; the clamp {c['ref']} ({comps[c['ref']].get('value')}, "
                 f"{float(c['row']['vf_max']):g} V at "
                 f"{c['row']['if_ma']:g} mA) to {c['far']} lets it reach "
                 f"{hit['after']:.3g} V" if c else "")
        kname = {"clamp": "offboard_input_clamp",
                 "rail": "offboard_input_rail_absent",
                 "over": "offboard_input_over"}[kind]
        fix = {"clamp": "clamp with Schottky diodes (or a part whose "
                        "drop is inside the margin), or add series "
                        "resistance so the pin's own limit holds",
               "rail": "reference the divider to a rail this board makes, "
                       "or clamp the pin within its rating",
               "over": "change the divider or clamp the pin"}[kind]
        vt(judged, "error", kname, hit["net"],
           refs + ([c["ref"]] if c and c["ref"] not in refs else []),
           f"'{hit['net']}' leaves the board {where} into {pin}, rated "
           f"{lim}: {why} the resistors take it to {hit['level']:.3g} V"
           f"{ctext}. {fix[0].upper()}{fix[1:]} [{src}"
           + (f"; {' '.join(c['row']['source'].split())}" if c else "")
           + "]",
           level_v=round(hit["level"], 3), held_v=round(hit["after"], 3),
           vmin=hit["vmin"], vmax=hit["vmax"], absent_rail=hit["absent"])

    # 29. a pin whose role doesn't fit its net (class 6 of
    # docs/miss-taxonomy.md; errata corpus: flexgrid v4's USBLC6-2SC6 had
    # its GND pin on VBUS and VBUS on ground, rev B swapped them back)
    role_checked = 0
    for ref, found in _pin_role_nets(nets, comps, pl_rails):
        if ref == "checked":
            role_checked = found
            continue
        sev = next(s for s in ("error", "warning", "info")
                   if any(h[0] == s for h in found))
        said = "; ".join(f"{func} (pin {pn}, a {role} pin) on '{net}'"
                         for _, pn, func, role, net in found[:6])
        if sev == "info":
            msg = (f"{ref} ({comps[ref].get('value')}) has its battery "
                   f"backup pin grounded: {said}. Fine when the backup "
                   f"domain is unused (most RTC datasheets say tie VBAT to "
                   f"ground then); check that is meant")
        else:
            msg = (f"{ref} ({comps[ref].get('value')}) has a pin whose "
                   f"role doesn't fit its net: {said}. A ground pin on a "
                   f"supply rail or a supply pin on ground shorts the rail "
                   f"through the part or leaves it unpowered; check the "
                   f"symbol's pin map and the wiring against the datasheet")
        v(sev, "pin_role_net", found[0][4], [ref], msg,
          pins=[{"pin": pn, "function": func, "role": role, "net": net}
                for _, pn, func, role, net in found])

    # 30. a battery protector with its cell side and load side swapped
    # (errata corpus: netscore's FS312MH had GND on the board's ground and
    # BAT- on the cell's -; its rev B put GND on the cell)
    cell_checked = 0
    for ref, cpin, cnet, lpin, lnet in _protector_cell_side(nets, comps):
        if ref == "checked":
            cell_checked = cpin
            continue
        v("warning", "protector_cell_side", lnet, [ref],
          f"{ref} ({comps[ref].get('value')}) has its cell-side pin "
          f"{cpin} on the board ground '{cnet}' and its load-side pin "
          f"{lpin} on the cell's own terminal '{lnet}': the two are "
          f"swapped, so the protector's FETs carry no current and it "
          f"cuts nothing. {cpin} goes to the cell's -, {lpin} to the "
          f"ground the load returns to [ABLIC S-8261 Rev.5.5_00 Figure "
          f"10, the same split]",
          cell_net=cnet, load_net=lnet)

    # 31. an MCU's serial-wire debug pins (mcu_pins.yaml debug; errata
    # corpus: fc980c rev1 wired its SWDIO header pin to the F401's PA15,
    # not PA13)
    dbg_checked = 0
    dbg_ok = {(h[2], h[3], h[0]) for h in _debug_pins(
        nets, comps, comp_pins, _verified_only(mp_table))}
    for kind, row, ref, sig, pn, func, net in _debug_pins(
            nets, comps, comp_pins, mp_table):
        if kind == "checked":
            dbg_checked = row
            continue
        src = " ".join(row["source"].split())
        want = row["debug"][sig]
        shared = (row.get("shared") or {}).get(want)
        if kind == "wrong":
            vt((ref, sig, kind) in dbg_ok, "error", "debug_pin_wrong",
               net, [ref],
               f"'{net}' carries {sig} but reaches {ref} "
               f"({comps[ref].get('value')}) on {func} (pin {pn}); "
               f"{row['mpn']} has {sig} only on {want}, so the debugger "
               f"cannot reach the core [{src}]",
               mcu=ref, signal=sig, want_pin=want, part=row["mpn"])
            continue
        note = (f" On this part {want} is also {shared} at reset, so a "
                f"load that pulls it can change how it boots."
                if shared else "")
        vt((ref, sig, kind) in dbg_ok, "warning", "debug_pins_reused",
           net, [ref],
           f"{ref} ({comps[ref].get('value')}) uses its {sig} pin {func} "
           f"(pin {pn}) on '{net}' for other parts, with no debug header, "
           f"test point or SWD-named net on it: once firmware takes the "
           f"pin as GPIO the debugger can't attach, and recovering the "
           f"chip needs a connect-under-reset or the boot loader.{note} "
           f"Bring {sig} to a header or pad [{src}]",
           mcu=ref, signal=sig, part=row["mpn"])

    # 32. a load against its source's limit (reference/source_limits.yaml;
    # class 11 of docs/miss-taxonomy.md; errata corpus: flexgrid's 200 mA
    # TPS7A03 feeding an ESP32-S3 module that wants 500 mA)
    sl_table = (load_source_limits() if source_limits is None
                else source_limits)
    sl_checked = 0
    sw_checked = 0
    for hit in itertools.chain(
            _source_limits(nets, comps, comp_pins, sl_table, pl_rails),
            (("sw_" + h[0], h[1]) if h[0] == "checked" else h
             for h in _switched_loads(
                 nets, comps, comp_pins, sl_table, pl_rails,
                 constraints.get("switched_loads") or []))):
        if hit[0] == "sw_checked":
            sw_checked = hit[1]
        elif hit[0] == "checked":
            sl_checked = hit[1]
        elif hit[0] == "not_judged":
            not_judged.append(hit[1])
        else:
            kind, sev, net, refs, msg, rows, extra = hit
            vt(_sl_judged(rows), sev, kind, net, refs, msg, **extra)

    # 33. a control signal's active level across a connection, and an I/O
    # pin's supply domain against its net's level (lib/signal_domains.py,
    # classes 14 and 15 of the gaps list; built from datasheets and
    # planted faults)
    pol_pins, dom_pairs = _signal_rules(
        v, vt, not_judged, nets, comps, comp_pins, pl_rails, io_table,
        abs_table, pl_specs, io_tolerance)

    # 34-36. wiring against design intent (class 8 of
    # docs/miss-taxonomy.md, #1137). design_intent states what no design
    # file says: a module drawn with another's symbol, a bus member left
    # unread on purpose.
    intent = constraints.get("design_intent") or {}
    stated = {e["ref"]: e["is"] for e in intent.get("modules") or []
              if isinstance(e, dict) and e.get("ref") and e.get("is")}
    unread = {e["net"] for e in intent.get("bus_unread") or []
              if isinstance(e, dict) and e.get("net")}

    # 34. a named bus with one member that reaches the connector but not
    # the part reading the rest (errata corpus: zxdiag v1.1's Pico read
    # MREQ, RD and WR off the ZX edge but not IORQ; v1.2 added GPIO20)
    bus_checked = 0
    for gname, mem, net, ref, read, conns, src in _bus_members_dropped(
            nets, comps, comp_pins):
        if gname == "checked":
            bus_checked = mem
            continue
        if net in unread:
            continue
        v("warning", "bus_member_dropped", net, [ref] + conns,
          f"{ref} ({comps[ref].get('value')}) reads {', '.join(read)} of "
          f"the {gname} but not {mem}: '{net}' reaches "
          f"{', '.join(conns)} with the others and stops there. Without "
          f"{mem} the part can't tell the cycles apart; wire it to a free "
          f"pin, or say why not in constraints design_intent.bus_unread "
          f"[{src}]",
          group=gname, member=mem, master=ref, connectors=conns)

    # 35. a UART line named for its direction on a pin that runs the other
    # way (errata corpus: smart-lcc rev A, RX_FROM_AUX on the Nano RP2040
    # Connect's D8, which is UART1 TX)
    uart_checked = 0
    for ref, pn, func, fn, net, way, row in _uart_directions(
            nets, comps, comp_pins, mp_table, stated):
        if ref == "checked":
            uart_checked = pn
            continue
        part = (row or {}).get("mpn") or comps[ref].get("value")
        src = (f" [{' '.join(row['source'].split())}]"
               if row and row.get("source") else "")
        said = (f", as constraints design_intent.modules says"
                if ref in stated else "")
        v("warning", "uart_direction", net, [ref],
          f"'{net}' is named as this board's {way}, but it reaches {ref} "
          f"({part}{said}) on {func} (pin {pn}), which is {fn}: TX must "
          f"meet the far end's RX. Swap the two lines or rename the net"
          f"{src}",
          pin=pn, function=fn, way=way, part=part)

    # 36. an always-on bus transceiver whose DIR is a raw connector strobe
    # (errata corpus: pico-if1 v1.0's 74LS245, OE grounded and DIR on /RD)
    xcvr_checked = 0
    for ref, oe_pin, oe_net, dir_pin, dir_net, side, conns in \
            _host_bus_transceivers(nets, comps, comp_pins, pl_rails):
        if ref == "checked":
            xcvr_checked = oe_pin
            continue
        v("warning", "transceiver_drives_host_bus", dir_net,
          [ref] + conns,
          f"{ref} ({comps[ref].get('value')}) has OE (pin {oe_pin}) tied "
          f"to '{oe_net}', so it is always on, and DIR (pin {dir_pin}) on "
          f"'{dir_net}' straight from {', '.join(conns)}: every cycle of "
          f"that strobe it drives {len(side)} host lines "
          f"({', '.join(side[:4])}...) whoever is addressed, fighting the "
          f"part the host meant to read. Gate OE (or DIR) with this "
          f"board's own address decode [TI SN74LS245 (SDLS146) function "
          f"table: OE low enables, DIR sets the way]",
          oe_net=oe_net, dir_net=dir_net)

    # 37. a differential or paired line's P/N and pair order across a
    # connector, against the connector's stated pinout or a standard one
    # (lib/pair_order.py, #1217; built from the USB, Type-C and IEEE 802.3
    # pin tables and planted faults)
    pairs_checked = _pair_rules(v, not_judged, violations, nets, comps,
                                comp_pins, pl_rails,
                                constraints.get("connector_pinouts") or [])

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
        "mcu_pwm_pairs_checked": pair_checked,
        "mcu_input_only_parts_checked": io_checked,
        "i2c_buses_checked": len(buses),
        "i2c_devices_placed": sum(len(d) for d in buses.values()),
        "bus_lines_checked": len(lines),
        "pulls_vs_leakage_checked": pulls_checked,
        "reset_contention_found": contention_checked,
        "io_drive_pins_checked": iolv_checked,
        "power_good_pins_checked": pg_checked,
        "power_path_fets_checked": pp_checked,
        "power_switches_checked": ps_checked,
        "power_switches_not_judged": sw_nj,
        "battery_protectors_checked": prot_checked,
        "unverified_findings": len(not_judged),
        "offboard_inputs_checked": ob_checked,
        "symbol_sizes_checked": sz_checked,
        "symbol_pinmaps_checked": pm_checked,
        "symbol_pinmaps_by_table": pm_by_table,
        "channel_parts_checked": ch_checked,
        "mcu_i2c_parts_checked": i2c_checked,
        "offboard_inputs_judged": obi_judged,
        "pin_role_parts_checked": role_checked,
        "protector_cell_sides_checked": cell_checked,
        "debug_mcus_checked": dbg_checked,
        "source_limits_checked": sl_checked,
        "switched_loads_checked": sw_checked,
        "polarity_pins_checked": pol_pins,
        "io_domain_pairs_checked": dom_pairs,
        "bus_groups_checked": bus_checked,
        "uart_parts_checked": uart_checked,
        "transceivers_checked": xcvr_checked,
        "connector_pairs_checked": pairs_checked,
    }
    if not_judged:
        facts["not_judged"] = not_judged
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
    counted = facts.get("pins_connected", 0) + sum(
        v for k, v in facts.items() if k.endswith(("_checked", "_judged"))
        and isinstance(v, int) and not isinstance(v, bool))
    if not violations and not counted and not facts.get("nothing_to_judge"):
        facts["nothing_to_judge"] = (
            f"{Path(netlist).name}: no pin connected and no part any audit "
            "topic checks")
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
