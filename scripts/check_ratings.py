#!/usr/bin/env python
"""check_ratings.py - every pin's datasheet rating against the net it sits on (P4/P8).

One concern: a pin driven past what its datasheet allows - an over-voltage
input, a reversed rail, an input above its own supply, a regulator output
asked for more than it can give. No DRC sees this class; it is a respin.

Inputs (pure Python, no KiCad run):
 - the board (--pcb): footprint refs, their `LCSC` / `LCSC Part` property,
   and the net on every pad. A footprint with no LCSC code is keyed by its
   MPN (#1119: the `MPN` property, else the parts.json line naming its ref)
   when parts/<MPN>.json or a parts.json line with that mpn exists, as on a
   US-sourced board; its findings carry `part_key` "MPN <mpn>";
 - the parts directory (--parts, default <ws>/parts beside <ws>/kicad):
   parts.json (the P3 BOM, per-part `attributes`) and the per-part
   extraction <LCSC>.json that datasheet_extract.py validates (pinout +
   abs_max, optional pin_ratings); then hwde's library
   (reference/extractions), then --extractions DIR, a batch reading of
   absolute-maximum ratings that abs_max_batch.py writes (#1339);
 - rail voltages, first source wins per net: constraints.json `voltages`
   (--constraints; worst-case volts), the power tree's rail table
   (--power-tree, default <ws>/architecture/power_tree.md: a markdown table
   whose second header cell says V/volt), a fixed-output regulator's output
   pin (AMS1117-3.3, L78L33: the MPN's voltage on its VOUT/OUT power_out
   pin), then the net name (+5V, 3V3, +12V, -5V, VBUS = 5 V; GND*/VSS* = 0),
   then a power_out pin the part regulates itself (an internal LDO's DVDD)
   whose extraction row gives `internal_v`, on a net that holds only that
   pin and capacitors to ground: decoupling only, so the datasheet's
   internal value is the rail (#843). Anything else on the net (an outside
   supply, a load) leaves it unknown.
   A signal net with no voltage of its own takes the highest rail it is
   pulled up to through a 2-pad resistor.
   A net with no voltage of its own is bounded (#864) when everything on it
   is a resistor, a capacitor, an input pin, a clamp or a driver. A clamp is
   a 2-pad K/A part with a recommended K-vs-A max, or one parts.json or its
   MPN calls a unidirectional TVS/zener: its V(BR)/VZ top, else VC, else an
   absolute K-vs-A max (#867). A driver is an output pin of a part whose
   supplies are all known (#869). The net lies in the hull of what its feed
   resistors reach (a known or bounded net's span, else the board's rails)
   and its drivers' rails - a push-pull output's rail alone, unless a feed
   from a stated or driven net pulls it past (an open drain typed output),
   and a driver with no ground pin swings down to the board's lowest rail,
   not 0 V (#1006) - narrowed by each clamp from its other side: A in K - Vz .. K + a diode drop, K in
   A - a diode drop .. A + Vz (#868). Bounded nets feed each other until
   nothing moves. The other parts on it are judged at both ends of that
   bound (a warning: the bound is derived). The parts that make the bound
   are not judged against it: a driver is covered by its supply rating
   unless such a feed pulls the net past its rail, a
   clamp passes when the current its feed can force through it, times its
   Vz, stays under the parts.json Pd; over it, rating_over_current.

Ratings per pin: the extraction's structured `pin_ratings` entries first
(pins, kind voltage|current, level abs_max|recommended, min/max as a number
or "VDD+0.3", optional ref pin), else its free-text `abs_max` rows parsed:
a row names its pins (VIN, EN, "V(IN)", VEN -> EN, IIN -> IN), a class
("any other pin", "5 V tolerant" -> pins whose notes say FT/5 V tolerant,
"supply" -> power_in, "input"/"output"), or a pair (VGS, "BST-SW",
"with respect to SW" - rated against the ref pin's net, not ground). A row
whose param says operating/recommended is a recommended rating. Pulse,
transient, ESD, clamp, injected and differential-input rows are skipped.
A named pin outranks a class; the tightest bound of the best tier wins.

Findings (kind = violation `kind`, remediations under reference/remediations/):
 - rating_over_voltage (error): net above an absolute maximum - including a
   bound relative to the part's own supply (an input above supply);
   a 2-pad part's `Voltage Rated` attribute below the volts across it; an
   unlisted capacitor's rating read from its maker's voltage code in the MPN
   (reference/mlcc_voltage_codes.yaml: Samsung CL, Murata GRM/GCM/GCJ/GRT,
   Yageo CC/CX, KEMET C, Taiyo Yuden, Fenghua, TDK C; each series cited to
   the maker's part-numbering guide) or a voltage written in its Value field
   ("220u/16V", "_10V_"), the lower when the two differ (cap_code_rating,
   #1307). A diode's Value voltage is a clamp voltage, so it is not read.
 - rating_reverse_polarity (error): net below an absolute minimum (a
   negative single figure, as P-channel parts print, is that minimum), or
   a ground pin sitting on a positive rail. A 2-terminal diode with no
   datasheet read and no reverse-voltage attribute is rated from its part
   number's family (reference/diode_vr.yaml: 1N4148, 1N400x, 1N581x, SS1x,
   SS3x, B2xx, B581xW, BAT54L, MBRS140, MBR0520, SMAJ/SMBJ/SMCJ, SMF, ESD5Z,
   PESD5V0S1B and TI ESD parts; each family cited to a maker's datasheet;
   a TVS or ESD diode by its standoff VRWM, never its clamp, and a
   bidirectional one either way round; diode_code_rating, #1323). Zeners,
   LEDs and families with no row stay unrated.
   Either is a warning, not an error, when the net's volts came through a
   pull-up: that resistor may be a deliberate series limit into a clamped
   pin. A supply pin fed through a resistor is not given the rail's volts.
   A 2-terminal diode (D*/LED*) with no extraction is rated by a parts.json
   reverse-voltage attribute ("Reverse Voltage (Vr)"), its cathode found by
   pad name (the board's pinfunction, else the netlist <board>.net beside
   it) or else pad 1, KiCad's convention.
   Reversed input (warning, field `scenario`): each 2-net power input - a
   J/P/CN/X/BT part on one ground and one positive rail, a header, jack or
   battery that plugs in either way - is plugged in reversed: its rail goes
   to minus its volts, carried through lone series R/L/FB parts (a blocking
   diode draws no current), and every diode it then reverse-biases past its
   reverse rating is named. "The LED blocks reverse current" is checked
   against the LED's VR, not taken on trust.
 - rating_diode_forward (error): a 2-terminal diode (D*/LED*) wired to
   conduct all the time - its anode on a stated rail with its cathode on
   ground, or a TVS/Zener clamp (parts.json Type, or its MPN family) with
   its anode above its cathode, fitted backwards (#645). Cathode found as
   for the reverse check. A plain diode forward between two rails (OR-ing),
   a bidirectional clamp and a net whose volts came through a pull-up are
   left alone. Not a rating, so it does not count as rating the part.
 - rating_outside_recommended (warning): above a recommended max, or a
   supply pin below its recommended min.
 - rating_over_current (warning): a power_out pin whose net's
   constraints `power[].current_a` exceeds the pin's current rating.
   A fuse or PPTC (F*/FU*, or a parts.json hold-current attribute) carrying
   a constraints power[].current_a: over its rating at the ambient is an
   error, a fuse past 75 % of its rerated current (k_T, Littelfuse) or a
   PPTC whose Ihold at the ambient (typical Bourns/Littelfuse table) clears
   the current by under 10 % a warning (R-RAT-3, R-RAT-4).
 - rating_unrated (warning): a part with no rating to check a connected
   pin against - no extraction, no row that names or classes the pin, or a
   relative bound whose reference supply has no known voltage. One per
   part, listing the pins. Never a silent pass.
 - rating_no_attributes (error): a rated part (diode, fuse, regulator, cap,
   a power-IC family: charge pump, ideal diode, hot-swap controller)
   whose parts.json row has empty `attributes` and which nothing else rates:
   its limit was never compared, so the gate fails until the row is filled
   (parts_enrich.py). A U/VR/IC part whose MPN carries a known family's output
   code counts as a regulator here, whatever its words. One per BOM code,
   naming its refs. The check reads
   nothing but the row: no cache, no network. Its ledger fact stays unknown.
 - rating_over_voltage, also: a chip resistor's volts across it above its
   limiting element voltage (parts.json Voltage-Supply(Max), else the chip
   size's D/CRCW e3 Umax; R-RAT-1); a fuse or PPTC whose voltage rating is
   under the rail it sits on (it must hold the rail off once open); an
   unextracted regulator whose input or enable pad sits above the input
   rating its filled row gives (regulator_findings; no pad names: its
   highest-voltage net, unless the part steps up); an unextracted power IC
   whose input pad, from its family's datasheet table (POWER_IC_FAMILIES),
   sits above the top of its filled row's input range, the row's figure and
   not the datasheet's absolute maximum (power_ic_findings). Its
   rating_unrated says what became of the inputs: an input it can't compare, or
   else that they are within the row (power_ic_why_unrated).
 - rating_regulator_output (error): a regulator whose output pad sits on a
   rail its row (or a fixed output in the MPN, or a known family's MPN code)
   says it can't make, or on a stated rail of the wrong sign for a part its
   family or words call positive or negative. Unextracted: a pad named in the
   netlist. Extracted: a power_out pin named as an output, against a rail the
   board itself states (constraints, power tree, net name); its output may
   also come from the extraction. Adjustable (#832): the Vout its FB divider
   and datasheet Vref (reference/regulator_vref.yaml) make, worst case over
   both tolerances (engcalc.fb_vout), outside the rail the divider's top
   sits on or the vout_v of a constraints block that names the ref; a
   constraints `voltages` figure is the rail's worst case, so it fits from
   the window's bottom to 10 % over the nominal (worst_case_fits); FB
   straight on a stated rail makes Vref, judged only for a part nothing
   calls fixed.
 - rating_regulator_output_unjudged (warning): a regulator where nothing says
   what it makes. Extracted: with an output pin on the board. Unextracted:
   with a filled row. Adjustable: a divider on FB with no Vref row for the
   part.
 - rating_over_temperature (error): the board's ambient above a part's
   operating range (parts.json "Operating Temperature", or the
   extraction's operating_temp_c), or constraints ambient_min_c below it
   (R-REL-2). The ambient is constraints ambient_c, else the enclosure air
   check_thermal works out, else 25 C, said so in `ambient`.
 - rating_datasheet_suspect (warning): a part whose datasheet PDF is a host
   placeholder or was judged suspect (index.json: cut off, no text layer, nothing
   ties it to the MPN; #706). The PDFs on disk are judged when the index is read
   (#768). The ledger facts read from that PDF (the extraction's source_pdf
   says which one) are unknown, never verified; a rating that came from the
   distributor or a package table was not read from it and is untouched (#781).
 - rating_datasheet_unassessed (warning, one per board): parts whose datasheet
   can't be judged here: an index.json entry with no PDF on disk, or a PDF with
   no MPN to check it against. `datasheet_extract --fetch` gets the PDF (#755).
 - rating_no_datasheet_read (warning, one per board): parts whose rating fact
   is `listed`, a number compared with no datasheet behind it (#781). It names
   the way to `verified`: `datasheet_extract --fetch`, then extraction.
 - rating_listing_disagrees (warning, one per part): the distributor's voltage
   rating and the datasheet's differ by over 10 % on the same rating (#799),
   whatever the rail. `side` high means the listing overstates the part, the
   direction a `listed` rating can't catch. `check_used` says which number the
   rating check read: `datasheet` (an extracted part is never judged on its
   listing), or `none` when no pin met a known rail. Every comparison is in
   facts `listing_comparison`.
 - rating_over_clamp (warning): on a rail of known voltage that a
   unidirectional TVS holds to ground (or a constraints `voltages` entry's
   clamp_v, with breakdown_v), a part's rating judged at the TVS's breakdown
   max (`level` breakdown: no surge current yet) and at its clamping voltage
   (`level` clamp: a surge at its rated peak pulse current), not the nominal
   (#500). The TVS's numbers come from its extraction's V(BR)/VC rows, its
   listing's clamping voltage, else its part number's family in
   reference/diode_vr.yaml. Facts `clamped_rails` names each rail, its clamp
   and the parts judged on it.
Sources and verified/disputed tags: docs/ingest/safety-components.md.
A pin on a net of unknown voltage is not a finding (counted in facts); a
supply pin on one is named in not_judged by rail, since nothing compared it
(#601). A connected pad the extraction's pinout has no row for is
rating_unrated. A board where nothing could be compared with known volts
reports nothing_to_judge, led by the reason that kept out the most parts (no
part number, a passive with no voltage field, no datasheet read, a datasheet
with no pin rating, or no known voltage on its nets), and facts
`parts_not_compared` lists each part under its reason (#1330). Facts `parts_rated_unextracted` names each part
compared with no extraction behind it (a distributor's listing, a part-number
voltage code or diode family, a package table), which pins_checked does not count (#1307).

The fact ledger (lib/ledgerlib.py): each refdes gets a `rating` fact (verified
when a datasheet's rating was compared with known volts and held, listed when
the number was the distributor's, a capacitor's part-number voltage code or
Value field's or a diode's part-number family (part_code), or a package
table's, `rated_from` says which;
failed on an error finding, unknown when unrated or nothing could be compared)
and a
`pinout` fact (pinout_fact: the board's pad numbers and names against the
extraction's pinout; no extraction is unknown). Written into <ws>/ledger.json
when the board sits in a workspace; `ledger_counts` in the report.

Pins go by where the copper puts them (#843): when the extraction has a land
drawing (land_pattern.pads, top-view places) and its places fall on the
footprint's pads under one quarter turn only, each pad is read as the pin
drawn at its place, whatever its number, before any rating, name or pinout
fact is read. A symbol and footprint renumbered alike (a mirrored drawing)
then rate right, and a footprint whose numbers agree with the datasheet but
whose pads sit on other pins rates by the copper. Facts `pins_by_position`
({ref: {pad: pin}}, the pads that moved) and `pins_by_number_why` (a drawing
that could not settle it, so pins kept their numbers).

CLI: --pcb board.kicad_pcb [--parts DIR] [--constraints c.json]
     [--power-tree power_tree.md] [--extractions DIR] [--out report.json]
Exit 0 pass / 1 findings / 2 error (SPEC 6 contract).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import sexpdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import engcalc  # noqa: E402
import mlcc  # noqa: E402
import diode_vr  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_ratings"
EPS = 1e-6

GROUND_RE = re.compile(r"^(?:[ADPSC]?GND|GND[A-Z0-9_]*|VSS[A-Z0-9_]*|0V|"
                       r"EARTH|RTN|[A-Z0-9_]*_RTN)$")
SKIP_RE = re.compile(r"ESD|HBM|CDM|TRANSIENT|PULSE|SURGE|NON-REPETITIVE|"
                     r"HIPOT|WITHSTAND|ISOLATION|DIELECTRIC|VARIATION|"
                     r"DIFFERENTIAL|NOT THIS PART|INJECT|CLAMP|SHORT|LATCH|"
                     r"FORWARD VOLTAGE|DROPOUT|THRESHOLD|TOTAL|IFSM|IDM|IPP|"
                     r"\b\d+\s*[NMU]S\b|"
                     # electrical characteristics that extractors park in
                     # abs_max: levels, swings, drops - never limits
                     r"ELEC|HYST|SWING|HIGH-LEVEL|LOW-LEVEL|\bVOH\b|"
                     r"\bVOL\b|\bVIH\b|\bVIL\b|VENH|VENL|BREAKDOWN|BVDSS|"
                     r"\bDROP|DIFF|POWER.ON|\bTYP\b|RIPPLE|OFFSET|LEAKAGE|"
                     r"NOISE|ACCURACY|REGULATION|REFERENCE VOLTAGE|"
                     r"FEEDBACK VOLTAGE")
REC_RE = re.compile(r"OPERATING|RECOMMENDED")
DIODE_ALIASES = {"ANODE": "A", "CATHODE": "K", "GATE": "G", "DRAIN": "D",
                 "SOURCE": "S", "COLLECTOR": "C", "EMITTER": "E", "BASE": "B"}
IO_TYPES = {"input", "analog", "clock", "bidirectional", "unspecified",
            "passive", "output", "tristate", "open_collector"}
IN_TYPES = {"input", "analog", "clock", "bidirectional", "unspecified"}
OUT_TYPES = {"output", "tristate", "open_collector", "bidirectional"}
REG_OUT_NAMES = {"VOUT", "OUT", "VO", "OUTPUT"}
# A 2-pad part's working-voltage attribute as LCSC lists it: "Voltage Rated"
# and "Voltage Rating" (caps, connectors), "Voltage - DC", and the chip
# resistors' "Voltage-Supply(Max)" / "Working Voltage". Not "Voltage -
# Forward(Vf)" or "Overload Voltage": neither is a rating across the part.
V_ATTR_RE = re.compile(r"voltage.*rated|rated.*voltage|voltage rating|"
                       r"voltage - dc|voltage\s*-\s*supply\s*\(max\)|"
                       r"working voltage", re.I)


# --- board ------------------------------------------------------------------

def _t(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and c and _t(c[0]) == name]


MPN_PROPS = ("MPN", "Manufacturer Part Number", "Manufacturer_Part_Number",
             "Part Number", "PN")


def load_parts_on_board(pcb: Path) -> list[dict]:
    """[{ref, lcsc, mpn, pads{number: net}}] from the .kicad_pcb text."""
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CheckError(f"cannot parse {pcb}: {exc}") from exc
    table = {}
    for n in _kids(root, "net"):
        if len(n) >= 3:
            table[n[1]] = str(n[2])
    out = []
    for fp in _kids(root, "footprint"):
        props = {}
        for p in _kids(fp, "property"):
            if len(p) >= 3 and isinstance(p[1], str):
                props[p[1]] = str(p[2])
        pads, fns, nums, xy = {}, {}, set(), {}
        bottom = any(str(_t(x)).startswith("B.")
                     for x in (_kids(fp, "layer") or [[None, ""]])[0][1:2])
        for pad in _kids(fp, "pad"):
            num = str(pad[1]) if len(pad) > 1 else ""
            if num:
                nums.add(num)
                at = (_kids(pad, "at") or [[]])[0]
                if len(at) >= 3 and all(isinstance(_t(c), (int, float))
                                        for c in at[1:3]):
                    # the pad in its footprint's own frame; KiCad stores a
                    # flipped footprint's pads mirrored, so undo that to
                    # get the part's own top view
                    xy.setdefault(num, []).append(
                        (float(at[1]), -float(at[2]) if bottom
                         else float(at[2])))
            net = None
            for nn in _kids(pad, "net"):
                strs = [x for x in nn[1:] if isinstance(x, str)]
                net = strs[-1] if strs else table.get(nn[1])
            for pf in _kids(pad, "pinfunction"):
                if num and len(pf) > 1 and isinstance(pf[1], str):
                    fns.setdefault(num, pf[1])
            if num and net and not net.startswith("unconnected-"):
                pads.setdefault(num, net)
        attrs = {str(_t(x)) for a in _kids(fp, "attr") for x in a[1:]}
        dnp = "dnp" in attrs or any(str(_t(x)).lower() == "yes"
                                    for a in _kids(fp, "dnp") for x in a[1:])
        out.append({"ref": props.get("Reference", "?"), "dnp": dnp,
                    "in_bom": not (attrs & {"exclude_from_bom", "board_only"}),
                    "lcsc": (props.get("LCSC") or props.get("LCSC Part")
                             or "").strip(),
                    "mpn": next((props[k].strip() for k in MPN_PROPS
                                 if (props.get(k) or "").strip()), ""),
                    "value": props.get("Value", ""),
                    "footprint": str(fp[1]) if len(fp) > 1 else "",
                    "pads": pads, "fns": fns, "nums": sorted(nums),
                    "pad_xy": {k: (sum(x for x, _ in v) / len(v),
                                   sum(y for _, y in v) / len(v))
                               for k, v in xy.items()}})
    return out


POS_TOL_MM = 0.1  # turns within this of the best fit are equally good


def pin_map(b: dict, ext: dict | None) -> tuple[dict | None, str | None]:
    """(pad -> extraction pin, None) when the extraction's land drawing
    (land_pattern.pads, top-view at_mm) places the footprint's pads on other
    pins than their numbers, else (None, why not or None). Pins are matched
    the way the copper maps them, not by number (#843): a symbol and
    footprint renumbered alike (hifi-receiver's PLR237, pads 1 and 3 the
    drawing's 3 and 1) put the datasheet's pin 1 on the footprint's pad 3.
    Only quarter turns are tried, never the mirror: a part on its own side
    can be turned, not flipped. The map is used only when every turn that
    fits gives the same one, so a symmetric row of pins with nothing to fix
    its turn keeps matching by number."""
    stated = ((ext or {}).get("land_pattern") or {}).get("pads") or []
    got = b.get("pad_xy") or {}
    if not stated or not got:
        return None, None
    import fp_verify as fpv
    acc: dict[str, list] = {}
    for q in stated:
        try:
            acc.setdefault(str(q["number"]), []).append(
                (float(q["at_mm"][0]), float(q["at_mm"][1])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    # a number drawn twice (a SOT-223's lead and tab) is its places' mean
    ref_all = {k: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v))
               for k, v in acc.items()}
    common = sorted(set(ref_all) & set(got))
    if len(common) < 2:
        return None, None
    maps = fpv._clean({k: ref_all[k] for k in common},
                      {k: got[k] for k in common}, POS_TOL_MM)
    if not maps:
        return None, ("its pads don't fall one to one on the land "
                      "drawing's places, so pins were matched by number")
    if len({tuple(sorted(m.items())) for _, m in maps}) > 1:
        return None, ("the land drawing fits its pads at more than one turn "
                      "with different pins, so pins were matched by number")
    moved = {k: r for k, r in maps[0][1].items() if k != r}
    return (moved or None), None


def apply_pin_map(b: dict, moved: dict) -> None:
    """Renumber a board part's pads, names and numbers into the
    extraction's pin space (pin_map's {pad: pin})."""
    b["pads"] = {moved.get(k, k): v for k, v in b["pads"].items()}
    b["fns"] = {moved.get(k, k): v for k, v in b["fns"].items()}
    b["nums"] = sorted(moved.get(k, k) for k in b["nums"])
    b["pin_map"] = dict(sorted(moved.items()))


# --- net voltages -------------------------------------------------------------

def _safe_name(key: str) -> bool:
    return bool(key) and not re.search(r"[/\\]|^\.", key)


def value_mpn(b: dict) -> str:
    """The footprint's Value when it reads as a part number (#815): one
    word of at least four characters with a letter and a digit
    (TPS62086, AMS1117-3.3, 74LVC1G3157), not a component value like
    3.3V or 100nF. Many boards carry an IC's number only there. Else ''."""
    v = str(b.get("value") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_#-]{3,}", v):
        return ""
    if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?[A-Za-z]{0,3}", v):
        return ""
    return v


def key_by_mpn(board: list[dict], bom: list[dict], parts_dir: Path,
               bom_by_lcsc: dict,
               extractions: Path | None = None) -> dict[str, str]:
    """#1119: give a footprint with no LCSC code its MPN as the part key
    when a record is found under it - parts/<MPN>.json (or the library's),
    or a parts.json line with that mpn and no lcsc, which then joins
    bom_by_lcsc under the MPN. The MPN is the footprint's MPN property,
    else the mpn of the parts.json line that lists its ref, else (#815)
    its Value when that reads as a part number and a record is on disk
    under it (a parts.json line alone doesn't key a Value). {ref: mpn}."""
    from mpn_decode import norm
    by_ref = {r: p for p in bom for r in (p.get("refs") or [])
              if isinstance(r, str)}
    by_mpn = {norm(p["mpn"]): p for p in bom
              if p.get("mpn") and not str(p.get("lcsc") or "").strip()}
    keyed = {}
    for b in board:
        if b["lcsc"]:
            continue
        mpn = b.get("mpn") or str((by_ref.get(b["ref"]) or {}).get("mpn")
                                  or "").strip()
        from_value = not mpn
        mpn = mpn or value_mpn(b)
        if not mpn:
            continue
        line = None if from_value else by_mpn.get(norm(mpn))
        # the record's file name: the MPN as written, knowledgelib's safe
        # name ('#' and '/' made '-'), or that with '.' made '-' too, as
        # hand-made records are named (ABM8G-24.000MHZ-18-D2Y-T is stored
        # as ABM8G-24-000MHZ-18-D2Y-T.json)
        stems = [x for x in (mpn, re.sub(r"[^A-Za-z0-9._-]", "-", mpn),
                             re.sub(r"[^A-Za-z0-9_-]", "-", mpn))
                 if _safe_name(x)]
        if not stems:
            continue
        on_disk = next((s for s in stems
                        for d in _ext_dirs(parts_dir, extractions)
                        if (d / f"{s}.json").is_file()), None)
        if not (on_disk or line):
            continue
        mpn = on_disk or stems[0]
        b["lcsc"] = mpn
        if line is not None:
            bom_by_lcsc.setdefault(mpn, line)
        keyed[b["ref"]] = mpn
    return keyed


def base_net(net: str) -> str:
    return net.rsplit("/", 1)[-1].upper()


def is_ground(net: str) -> bool:
    return bool(GROUND_RE.match(base_net(net)))


# A name word that makes the net a signal about a rail, not the rail:
# PG_1V35 is 1V35's power-good, ADC_1V35 its divided-down sense.
SIGNAL_WORDS = {"PG", "PGOOD", "PGD", "PWRGD", "PWROK", "POK", "OK", "ADC",
                "AIN", "SENSE", "SNS", "VSENSE", "ISENSE", "FB", "EN", "ENA",
                "ENABLE", "MON", "DIV", "DET", "FLT", "FAULT", "ALERT",
                "MEAS", "CTRL", "INT", "IRQ", "SEL", "TRIP", "OVP", "UVP",
                "SS", "TRK", "TRACK", "COMP", "ILIM"}


def volts_from_name(net: str) -> float | None:
    """A rail's voltage read from its name: +3V3, 1V35, VCC_5V, VBUS and
    VBUS_J1 (any VBUS word) as 5 V, grounds 0. A name with a signal word
    (PG_1V35, ADC_1V35, VBUS_SENSE, 3V3_EN) is a signal, not a rail."""
    n = base_net(net)
    if GROUND_RE.match(n):
        return 0.0
    words = n.split("_")
    if SIGNAL_WORDS & set(words):
        return None
    if n.endswith("VBUS") or "VBUS" in words:
        return 5.0
    m = re.search(r"(?<![A-Z0-9.])([+-])?(\d+)V(\d+)(?![0-9])", n)
    if m:
        v = float(f"{m.group(2)}.{m.group(3)}")
        return -v if m.group(1) == "-" else v
    m = re.search(r"(?<![A-Z0-9.])([+-])?(\d+(?:\.\d+)?)V(?![A-Z0-9])", n)
    if m:
        v = float(m.group(2))
        return -v if m.group(1) == "-" else v
    return None


_78XX_DECIMAL = {"33": 3.3, "52": 5.2, "85": 8.5}


def reg_out_volts(mpn: str) -> float | None:
    """Fixed output of a regulator MPN: AMS1117-3.3 -> 3.3, L78L33 -> 3.3,
    L7815 -> 15. A 78xx code counts only as the MPN's own prefix (L, LM,
    MC, UA, KA, NCV, CJ) and reads as whole volts, bar the decimal codes
    33 = 3.3, 52 = 5.2 and 85 = 8.5; any other
    MPN (HT7850, TPS78233) is unknown, never a guess."""
    m = re.search(r"[-_](\d{1,2}\.\d{1,2})(?![0-9])", mpn)
    if m:
        return float(m.group(1))
    m = re.match(r"(?:L|LM|MC|UA|KA|NCV|CJ)?78L?M?(\d{2})", mpn.upper())
    if m:
        return _78XX_DECIMAL.get(m.group(1), float(m.group(1)))
    return None


# A fixed regulator's output, coded in tenths of a volt inside the MPN of a
# family known to use the code. Not reg_out_volts: that one also names rails
# and regulators for the whole check, while these only ever judge a part's
# output pin (extracted_output_findings, regulator_findings) or say a bare
# row needs filling (no_attribute_findings).
_REG_CODE_RES = [re.compile(p) for p in (
    r"RT9\d{3}[A-Z]?-(\d\d)",               # Richtek: RT9080-33GJ5
    r"(?:TLV|TPS)7\d\d(\d\d)(?!\d)",        # TI: TLV75733P, TPS78233
    r"TPS7[AB]\d\d(\d\d)(?!\d)",            # TI: TPS7A0233, TPS7B6950
    r"XC62\d\d[A-Z](\d\d)\d",                # Torex: XC6206P332 (then accuracy)
    r"HT7[1-8](\d\d)(?!\d)",                 # Holtek: HT7333-A
    r"ME6\d{3}[A-Z](\d\d)",                  # Microne: ME6211C33M5G
)]
# not 01 or 00: those families' adjustable parts
_REG_CODES = {"10", "11", "12", "15", "18", "25", "27", "28", "29", "30", "31",
              "33", "36", "45", "50"}
# A fixed part whose number isn't a tenths code. Its FB pad senses the output
# directly: the datasheet's VFB is 4.95-5.05 V on the AP63205.
_REG_FIXED_PARTS = {"AP63203": 3.3, "AP63205": 5.0}     # Diodes AP6320x, p. 1


def reg_fixed_part(mpn: str) -> float | None:
    """What a fixed part in _REG_FIXED_PARTS makes: AP63205WU-7 -> 5.0."""
    mpn = mpn.upper()
    return next((v for p, v in _REG_FIXED_PARTS.items()
                 if mpn.startswith(p)), None)


def reg_out_code(mpn: str) -> float | None:
    """Fixed output in a known family's MPN: TLV75733P -> 3.3, AP63205 -> 5.0,
    and None for an adjustable part's code or any other family (TPS54233 is an
    adjustable buck, TLV7031 a comparator)."""
    mpn = mpn.upper()
    fixed = reg_fixed_part(mpn)
    if fixed is not None:
        return fixed
    for rx in _REG_CODE_RES:
        m = rx.match(mpn)
        if m:
            return int(m.group(1)) / 10 if m.group(1) in _REG_CODES else None
    return None


# Switching regulators whose number is all a bare row says about them (#763,
# #774). Each family's datasheet calls it a step-down or boost converter; none
# names a load switch, an e-fuse or a charge pump, which share the TPS and TLV
# prefixes. It says what a part is, never what it's rated for or makes.
_STEP_UP_FAMILY_RE = re.compile(
    r"TPS6104[01](?!\d)")   # TI TPS61040/41: 1.8-6 V input boost
_SWITCHER_FAMILY_RE = re.compile(
    r"AP6320[0135]|"        # Diodes AP63200/01/03/05: 3.8-32 V, 2 A sync buck
    r"TPS54\d{3}|"          # TI TPS54233, TPS54302, TPS54560B: step-down
    r"TLV6256\d|"           # TI TLV62568, TLV62569: 2.5-5.5 V sync buck
    r"LMR33\d{3}|"          # TI LMR33630: sync step-down
    r"LM5017(?!\d)|"        # TI LM5017: 7.5-100 V COT sync buck / Fly-Buck
    r"LM5164(?!\d)|"        # TI LM5164: 6-100 V, 1 A sync buck
    + _STEP_UP_FAMILY_RE.pattern)


def reg_by_family(ref: str, mpn: str) -> bool:
    """A U/VR/IC part whose MPN makes it a regulator, for judging its own row
    only: a fixed output's code, or a switcher family. Not is_regulator: that one
    also names rails for the whole check."""
    return bool(REG_REF_RE.match(ref)) and (
        reg_out_code(mpn) is not None or
        _SWITCHER_FAMILY_RE.match(mpn.upper()) is not None)


# Power ICs that aren't regulators, whose number is all a bare row says about
# them (#797). Each family's datasheet says what it is and which pads are its
# inputs (space-separated where a name has several pads), with the pin count of
# the package, exposed pad included (#805). A part sold in several packages gets
# a row per package, picked by the code in its MPN, since its input moves (#807).
# The table holds no rating and names no rail: the limit is the filled row's
# alone. A filled row's top is the datasheet's recommended maximum, not its
# absolute maximum, and it stays the bound: a rail between the two is outside
# what the part is specified to run on. One row reads lower still: a TPS3823-33's
# listing gives its threshold options ("2.5V、3V、3.3V、5V"), so its top is 5 V
# under the datasheet's 5.5 V. A rail between them is a finding an extraction or a
# hand-verified rating answers.
# A row suits a part with one supply. Parts whose pins have limits of their own
# (current monitors, gate drivers, SN6505B) get an extraction with pin_ratings
# instead (#815).
POWER_IC_FAMILIES = (
    {"mpn": re.compile(r"TPS6040[0-3](?!\d)"),
     # "Unregulated 60-mA Charge Pump Voltage Inverter"
     "input": {"IN": "2"}, "pins": 5,
     "datasheet": "TI SLVS324C Table 6-1, DBV"},
    {"mpn": re.compile(r"LM66200(?!\d)"),
     # "2.5-A Dual Ideal Diode"
     "input": {"VIN1": "3", "VIN2": "6"}, "pins": 8,
     "datasheet": "TI SLVSG04 Table 5-1, DRL"},
    {"mpn": re.compile(r"LM5069(?!\d)"),
     # "Hot Swap and In-Rush Current Controller"
     "input": {"VIN": "2"}, "pins": 10,
     "datasheet": "TI SNVS452G Pin Functions, DGS"},
    {"mpn": re.compile(r"TPS2051BDBV"),
     # "TPS20xxB Current-Limited, Power-Distribution Switches"
     "input": {"IN": "5"}, "pins": 5,
     "datasheet": "TI SLVS514Q Table 5-1, SOT-23"},
    {"mpn": re.compile(r"TPS2051BD(?:GN|R|$)"),
     "input": {"IN": "2 3"}, "pins": 9,
     "datasheet": "TI SLVS514Q Table 5-1, SOIC and DGN"},
    {"mpn": re.compile(r"TPS2051CDBV"),
     # "TPS20xxC and TPS20xxC-2 Current Limited, Power-Distribution Switches"
     "input": {"IN": "5"}, "pins": 5,
     "datasheet": "TI SLVSAU6I Table 5-2, DBV"},
    {"mpn": re.compile(r"SY6280AAC"),
     # "SY6280/SY6280A, 5.5V, 2A Low Loss Power Distribution Switch"
     "input": {"IN": "5"}, "pins": 5,
     "datasheet": "Silergy SY6280 application note, Pin Description, SOT23-5"},
    {"mpn": re.compile(r"TPS3808G01DBV"),
     # "TPS3808 Low-Quiescent-Current, Programmable-Delay Supervisory Circuit";
     # SENSE watches a rail of its own and isn't an input here
     "input": {"VDD": "6"}, "pins": 6,
     "datasheet": "TI SBVS050N Table 5-1, SOT-23"},
    {"mpn": re.compile(r"TPS3823-33DBV"),
     # "TPS382x Voltage Monitor With Watchdog Timer"
     "input": {"VDD": "5"}, "pins": 5,
     "datasheet": "TI SLVS165O Table 5-1, TPS3823 DBV"},
    {"mpn": re.compile(r"TPS22918DBV"),
     # "TPS22918 5.5-V, 2-A, 52-mΩ On-Resistance Load Switch"
     "input": {"VIN": "1"}, "pins": 6,
     "datasheet": "TI SLVSD76C Pin Functions, DBV"},
    {"mpn": re.compile(r"TPS26631RGE"),
     # "TPS2663x 60V, 6A Power Limiting, Surge Protection
     # Industrial eFuse", with IN_SYS as its second supply pin
     "input": {"IN": "1 2", "IN_SYS": "5"}, "pins": 25,
     "datasheet": "TI SLVSE94G Table 5-1, VQFN"},
    {"mpn": re.compile(r"TPS26631PWP"),
     "input": {"IN": "1 2 3", "IN_SYS": "6"}, "pins": 21,
     "datasheet": "TI SLVSE94G Table 5-1, HTSSOP"},
)


def power_ic_row(mpn: str) -> dict | None:
    """The family table's row for this MPN, or None."""
    return next((r for r in POWER_IC_FAMILIES if r["mpn"].match(mpn.upper())),
                None)


def power_ic_by_family(ref: str, mpn: str) -> bool:
    """A U/VR/IC part in a power-IC family: regulator_findings stays out, so its
    filled row isn't judged as a regulator's."""
    return bool(REG_REF_RE.match(ref)) and power_ic_row(mpn) is not None


# Families whose sign the MPN settles. Unknown is the default: a part in
# neither list is compared by magnitude only. Negative is asked first, since
# TPS72325 reads as a positive TI code.
_NEG_REG_RE = re.compile(
    r"(?:L|LM|MC|UA|KA|NCV|CJ)?79L?M?\d\d|LM337|LM299[01]|ADP718[235]|"
    r"LT(?:1033|1175|1964|3015|309[0134])|TPS7A3[034]|TPS723\d\d|"
    r"MIC527[01]|MAX1735")
_POS_REG_RE = re.compile(
    r"(?:L|LM|MC|UA|KA|NCV|CJ)?78L?M?\d\d|[A-Z]{0,4}1117|AP2112|LP5907|"
    r"LP2985|MIC5219")
_POLARITY_ATTR_RE = re.compile(r"polarity|output config|topology", re.I)


def reg_sign(mpn: str, bom: dict, desc: str = "") -> tuple[int, str] | None:
    """(+1 or -1, what says so) for a regulator, or None when nothing does or
    the words say both (a dual). The MPN's negative families, then the row's
    and the extraction's words, a negative Output Voltage, then the positive
    families."""
    mpn = mpn.upper()
    if _NEG_REG_RE.match(mpn):
        return -1, f"its family, {mpn}"
    atts = bom.get("attributes") or []
    text = " ".join([desc] + [str(bom.get(k) or "") for k in REG_ROW_TEXT] + [
        str(a.get("value") or "") for a in atts
        if _POLARITY_ATTR_RE.search(str(a.get("name") or ""))])
    neg = re.search(r"\bnegative\b", text, re.I) or any(
        VOUT_ATTR_RE.search(str(a.get("name") or "")) and
        re.match(r"\s*-\s*\d", str(a.get("value") or "")) for a in atts)
    pos = re.search(r"\bpositive\b", text, re.I)
    if neg or pos:
        if neg and pos:
            return None
        return (-1, "its description") if neg else (1, "its description")
    if _POS_REG_RE.match(mpn) or reg_out_code(mpn) is not None:
        return 1, f"its family, {mpn}"
    return None


def cell_volts(cell: str) -> list[float]:
    """Voltages in a power-tree volts cell: '5.0 / 4.7' -> [5.0, 4.7];
    a range '4.5-5.5' (or 'to', '..') is its upper end; tolerance terms
    ('1.8 +-5%', '3.3 +/-0.1', '5%') are dropped, leaving the nominal."""
    s = cell.replace("\u2013", "-").replace("\u00b1", "+/-")
    s = re.sub(r"\+\s*/?\s*-\s*\d+(?:\.\d+)?\s*%?", " ", s)
    s = re.sub(r"\d+(?:\.\d+)?\s*%", " ", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*V?\s*(?:-|to|\.\.)\s*(\d+(?:\.\d+)?)",
               lambda m: str(max(float(m.group(1)), float(m.group(2)))), s,
               flags=re.I)
    return [float(x) for x in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", s)]


def power_tree_volts(path: Path, nets: set[str]) -> dict[str, float]:
    """Rail table rows of power_tree.md: | +3V3 | 3.3 | ... (header cell 2
    must say V / volt); '/VIN, +5V | 5.0 / 4.7' pairs names with numbers."""
    out: dict[str, float] = {}
    if not path or not path.is_file():
        return out
    by_base = {}
    for n in nets:
        by_base.setdefault(n.lstrip("/"), n)
    rows = [ln.strip() for ln in path.read_text(encoding="utf-8",
                                               errors="replace").splitlines()]
    header = None
    for i, ln in enumerate(rows):
        if not ln.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if i + 1 < len(rows) and re.match(r"^\|[\s:|-]+\|?$", rows[i + 1]):
            header = cells
            continue
        if re.match(r"^\|[\s:|-]+\|?$", ln) or not header or len(cells) < 2 \
                or len(header) < 2 \
                or not re.search(r"\bV\b|volt", header[1], re.I):
            continue
        names = [by_base.get(x.strip().lstrip("/"))
                 for x in cells[0].split(",")]
        nums = cell_volts(cells[1])
        if not nums:
            continue
        for k, name in enumerate(names):
            if name:
                out.setdefault(name, nums[k] if len(nums) == len(names)
                               else max(nums))
    return out


# --- ratings --------------------------------------------------------------------

def aliases(name: str) -> set[str]:
    full = name.upper().strip()
    al = {full, re.sub(r"_\d+$", "", full), re.split(r"[-/]", full)[0] or full,
          re.sub(r"[^A-Z0-9]", "", full)}
    if full in DIODE_ALIASES:
        al.add(DIODE_ALIASES[full])
    m = re.match(r"^[AD](VDD|VCC)", full)   # AVDD/DVCC answer to VDD/VCC
    if m:
        al.add(m.group(1))
    return {a for a in al if a and not a.isdigit() and a not in ("V", "I")}


def parse_term(term: str, scale: float):
    """'4.0' -> 4.0; 'VDD+0.3' -> ('VDD', 0.3); '(V+)+0.3' -> ('V+', 0.3);
    'VSS-0.3' -> -0.3. None when unreadable."""
    t = term.strip().upper().replace(" ", "")
    try:
        return float(t) * scale
    except ValueError:
        pass
    if t.startswith("("):
        close = t.find(")")
        if close < 0:
            return None
        name, rest = t[1:close], t[close + 1:]
    else:
        m = re.match(r"([A-Z][A-Z0-9_]*)(.*)$", t)
        if not m:
            return None
        name, rest = m.group(1), m.group(2)
    off = 0.0
    if rest:
        m = re.match(r"^([+-])(\d+(?:\.\d+)?)$", rest)
        if not m:
            return None
        off = float(m.group(2)) * (-1 if m.group(1) == "-" else 1) * scale
    if GROUND_RE.match(name):
        return off
    return (name, off)


def parse_bounds(value, param: str, scale: float, rec: bool = False,
                 kind: str = "voltage"):
    """(lo, hi) from an abs_max value; either side None, a float or an expr.
    A positive 'min' is a lower bound only for a recommended row (an absolute
    maximum's floor is never above 0 V; BVDSS-style minimums are not limits)."""
    p = param.upper()
    if isinstance(value, (int, float)):
        v = float(value) * scale
        if kind == "voltage" and abs(v) < 1.0 and "ABS" not in p:
            return None, None   # a sub-volt single figure is a characteristic
        if v < 0:
            return v, None      # P-channel figures print negative: a floor
        # 'VCC supply (min -0.5)' = 6.5: the value is the max, the
        # parenthesis the table's own min column. A positive min (a
        # breakdown's) is no limit and falls through to the rule below.
        m = re.search(r"\(MIN\s+(-\d+(?:\.\d+)?|0(?:\.0+)?)\)", p)
        if m:
            return float(m.group(1)) * scale, v
        if re.search(r"(?<![A-Z])MIN(?![A-Z])", p):
            return (v, None) if v <= 0 or rec else (None, None)
        if re.search(r"(?<![A-Z])MAX(?![A-Z])", p):
            return None, v
        return (v, None) if v < 0 else (None, v)
    s = str(value).strip().replace("±", "+/-").replace("+-", "+/-")
    m = re.match(r"^\+/-\s*(\d+(?:\.\d+)?)$", s)
    if m:
        v = float(m.group(1)) * scale
        return -v, v
    parts = re.split(r"\s+to\s+", s, flags=re.I)
    if len(parts) == 2:
        return parse_term(parts[0], scale), parse_term(parts[1], scale)
    parts = [x for x in s.split("/")]
    if len(parts) in (2, 3):
        lo, hi = parse_term(parts[0], scale), parse_term(parts[-1], scale)
        return lo, hi
    single = parse_term(s, scale)
    if isinstance(single, float):
        return parse_bounds(single / scale, param, scale, rec, kind)
    return (None, single) if single is not None else (None, None)


def unit_kind(unit: str) -> tuple[str | None, float]:
    u = (unit or "").strip().upper()
    if re.search(r"AC|RMS|KV", u):
        return None, 0
    if u in ("V", "VDC", "V DC") or u.startswith("V "):
        return "voltage", 1.0
    if u.startswith("MV"):
        return "voltage", 1e-3
    for pre, sc in (("MA", 1e-3), ("UA", 1e-6), ("A", 1.0)):
        if u == pre or u.startswith(pre + " ") or u.startswith(pre + "("):
            return "current", sc
    return None, 0


def pin_index(pinout: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for p in pinout:
        for a in aliases(p["name"]):
            idx.setdefault(a, []).append(p)
    return idx


def classify_row(param: str, idx: dict, pinout: list[dict]):
    """-> (explicit pins set, class name or None, ref pin name or None)."""
    p = param.upper()
    toks = re.findall(r"[A-Z][A-Z0-9_]*", p)
    ref = None
    m = re.search(r"(?:RESPECT TO|REFERRED TO|\bTO|\bFROM)\s+\(?([A-Z][A-Z0-9_]*)",
                  p) or re.search(r"\b([A-Z][A-Z0-9_]*)\s*-\s*\(?"
                                  r"([A-Z][A-Z0-9_]*)", p)
    if m:
        cand = m.group(m.lastindex)
        hits = idx.get(cand) or idx.get(cand[1:] if cand[:1] in "VI" else "")
        if hits and not any(h["type"] == "ground" for h in hits):
            ref = hits[0]["name"]
    pins: set[str] = set()
    for tok in toks:
        hits = idx.get(tok)
        if not hits and len(tok) > 1 and tok[0] in "VI":
            hits = idx.get(tok[1:])
        if not hits and len(tok) == 3 and tok[0] == "V" \
                and idx.get(tok[1]) and idx.get(tok[2]):
            hits, ref = idx[tok[1]], idx[tok[2]][0]["name"]
        # JEDEC's third-terminal suffix: VDSS / VGSS (gate or drain shorted
        # to source), VCEO / VCBO / VEBO (third terminal open) name the same
        # two pins as VDS / VGS / VCE.
        if not hits and len(tok) == 4 and tok[0] == "V" \
                and tok[1] != tok[2] and (tok[3] == "O" or tok[3] == tok[2]) \
                and idx.get(tok[1]) and idx.get(tok[2]):
            hits, ref = idx[tok[1]], idx[tok[2]][0]["name"]
        for h in hits or []:
            if h["type"] != "ground" and h["name"] != ref:
                pins.add(h["pin"])
    if ref is None and "REVERSE" in p and idx.get("K") and idx.get("A"):
        pins, ref = {h["pin"] for h in idx["K"]}, idx["A"][0]["name"]
    if pins:
        return pins, None, ref
    if "TOLERANT" in p or "FT" in toks:
        return set(), "ft", None
    if re.search(r"OTHER PIN|ALL OTHER|ANY PIN|ALL PINS|ANY I/O", p):
        return set(), "other", None
    if "SUPPLY" in p or any(t in ("VCC", "VDD", "VS", "VCCIO", "VDDIO")
                            for t in toks):
        return set(), "supply", None
    has_in = "INPUT" in p or any(t in ("VI", "VIN", "VIO") for t in toks)
    has_out = "OUTPUT" in p or any(t in ("VO", "VOUT", "VIO") for t in toks)
    if has_in and has_out:
        return set(), "io", None
    if has_in:
        return set(), "input", None
    if has_out:
        return set(), "output", None
    return set(), None, None


def part_ratings(ext: dict) -> dict:
    """{pin: {(kind, level): [rows...]}} - rows {lo, hi, ref, tier, src}.
    Tier 0 structured, 1 named, 2 ft, 3 class, 4 'other'."""
    pinout = ext.get("pinout") or []
    idx = pin_index(pinout)
    by_pin: dict[str, dict] = {}

    def add(pins, kind, level, lo, hi, ref, tier, src):
        for pin in pins:
            by_pin.setdefault(pin, {}).setdefault((kind, level), []).append(
                {"lo": lo, "hi": hi, "ref": ref, "tier": tier, "src": src})

    for r in ext.get("pin_ratings") or []:
        pins = set()
        for name in r.get("pins") or []:
            pins |= {p["pin"] for p in pinout if p["pin"] == str(name)}
            pins |= {h["pin"] for h in idx.get(str(name).upper(), [])}
        level = "rec" if r.get("level") == "recommended" else "abs"
        lo = parse_term(str(r["min"]), 1.0) if r.get("min") is not None else None
        hi = parse_term(str(r["max"]), 1.0) if r.get("max") is not None else None
        add(pins, r.get("kind", "voltage"), level, lo, hi, r.get("ref"), 0,
            f"pin_ratings: {r.get('source') or r.get('pins')}")
    classes: list[tuple] = []
    for row in ext.get("abs_max") or []:
        param = str(row.get("param") or "")
        kind, scale = unit_kind(row.get("unit"))
        if kind is None or SKIP_RE.search(param.upper()):
            continue
        level = "rec" if REC_RE.search(param.upper()) else "abs"
        lo, hi = parse_bounds(row.get("value"), param, scale, level == "rec",
                              kind)
        if lo is None and hi is None:
            continue
        pins, cls, ref = classify_row(param, idx, pinout)
        src = f"{param} = {row.get('value')} {row.get('unit') or ''}".strip()
        if pins:
            add(pins, kind, level, lo, hi, ref, 1, src)
        elif cls:
            classes.append((cls, kind, level, lo, hi, src))
    ft = {p["pin"] for p in pinout
          if re.search(r"\bFT[A-Za-z]*\b|5\s?V[- ]tolerant",
                       p.get("notes") or "", re.I)}
    for cls, kind, level, lo, hi, src in classes:
        for p in pinout:
            t, pin = p["type"], p["pin"]
            if t in ("ground", "nc"):
                continue
            hit = {"ft": pin in ft,
                   "other": t in IO_TYPES and pin not in ft,
                   "supply": t == "power_in",
                   "input": t in IN_TYPES or (t == "power_in" and "IN" in
                                              p["name"].upper()),
                   "output": t in OUT_TYPES or (kind == "current"
                                                and t == "power_out"),
                   "io": t in IN_TYPES | OUT_TYPES}[cls]
            if hit:
                tier = {"ft": 2, "other": 4}.get(cls, 3)
                add([pin], kind, level, lo, hi, None, tier, src)
    # keep only the best tier per (pin, kind, level)
    for rows in by_pin.values():
        for key, lst in rows.items():
            best = min(r["tier"] for r in lst)
            rows[key] = [r for r in lst if r["tier"] == best]
    return by_pin


# --- the check ----------------------------------------------------------------

def _fmt(v: float) -> str:
    return f"{v:.3g}"


def _board_features(bom: list[dict]) -> set[str]:
    """refdes parts.json classes as board_feature (printed copper, logos)."""
    out = set()
    for ln in bom:
        refs = ln.get("refdes") or []
        refs = [refs] if isinstance(refs, str) else refs
        rc = ln.get("refdes_class") or {}
        for r in refs:
            if rc.get(r, ln.get("assembly_class")) == "board_feature":
                out.add(str(r))
    return out


# --- nets the board states no voltage for (#864) --------------------------

_OHM_MULT = {"": 1.0, "R": 1.0, "k": 1e3, "K": 1e3, "M": 1e6}
DIODE_DROP_V = 0.7
PD_ATTR_RE = re.compile(r"Pd|Power Dissipation", re.I)


def _ohms(value: str) -> float | None:
    """Ohms from a resistor Value (470k, 4k7, 10R, 2.2M), else None."""
    tok = str(value or "").split()[:1]
    if not tok:
        return None
    tok = tok[0].replace("\u03a9", "R").replace("\u2126", "R")
    m = re.fullmatch(r"(\d+)([RkKM])(\d+)", tok)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _OHM_MULT[m.group(2)]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([RkKM]?)", tok)
    return float(m.group(1)) * _OHM_MULT[m.group(2)] if m else None


def _watts(text: str | None) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m?)W\s*", text or "")
    if not m:
        return None
    return float(m.group(1)) * (1e-3 if m.group(2) else 1.0)


def _clamp_text_volts(ext: dict) -> float | None:
    """A TVS or zener's conducting voltage from its free-text rows (#867):
    the top of its V(BR)/VZ breakdown range, else its clamping voltage VC.
    None when it prints neither."""
    best: dict[int, float] = {}
    for row in ext.get("abs_max") or []:
        kind, scale = unit_kind(str(row.get("unit") or ""))
        if kind != "voltage":
            continue
        p = str(row.get("param") or "").upper()
        if re.search(r"\bV\s*\(?BR\)?|\bVZ\b|BREAKDOWN|ZENER VOLTAGE", p):
            tier = 0
        elif re.search(r"\bVC\b|CLAMPING VOLTAGE", p):
            tier = 1
        else:
            continue
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?",
                                             str(row.get("value")))]
        if nums:
            best[tier] = max(best.get(tier, 0.0), max(nums) * scale)
    for tier in (0, 1):
        if best.get(tier, 0.0) > 0:
            return best[tier]
    return None


def clamp_row(b: dict, ext: dict | None,
              bom: dict | None = None) -> tuple[str, float] | None:
    """A zener or TVS clamp: two pads named K and A. (K's pad, the voltage it
    clamps at) from, in order: a recommended K-vs-A max (#864); for a part
    that parts.json or its MPN calls a unidirectional TVS/zener, the top of
    its free-text V(BR)/VZ row, else its VC row, else an absolute K-vs-A max
    (#867). None otherwise: a plain diode's reverse rating is not a clamp."""
    if ext is None or len(b["pads"]) != 2:
        return None
    names = {p["pin"]: p["name"] for p in ext.get("pinout") or []}
    if sorted(names.get(k, "") for k in b["pads"]) != ["A", "K"]:
        return None
    kpin = next(k for k in b["pads"] if names.get(k) == "K")
    rows = part_ratings(ext).get(kpin, {})
    for r in rows.get(("voltage", "rec"), []):
        if r["ref"] == "A" and isinstance(r["hi"], float) and r["hi"] > 0:
            return kpin, r["hi"]
    kind, bi = clamp_kind(bom) if bom else (None, False)
    if kind is None:
        kind, bi2 = clamp_kind({"mpn": ext.get("mpn")})
        bi = bi or bi2
    if kind is None or bi:
        return None
    v = _clamp_text_volts(ext)
    if v is not None:
        return kpin, v
    for r in rows.get(("voltage", "abs"), []):
        if r["ref"] == "A" and isinstance(r["hi"], float) and r["hi"] > 0:
            return kpin, r["hi"]
    return None


def _supply_rail(b: dict, ext: dict | None, volts: dict,
                 floor: float = 0.0) -> tuple[float, float] | None:
    """(lo, hi) of a part's supplies: every power_in pin on the board on a
    known net (at least one), and its ground pins. None when any supply's
    net is unknown - the rail an output swings between is then unknown.
    With no ground pin on the board the low side is the board's lowest
    rail (floor), not 0 V: the part may sit on a negative rail (#1006)."""
    sup, gnd = [], []
    for p in (ext or {}).get("pinout") or []:
        net = b["pads"].get(p["pin"])
        if net is None or p["type"] not in ("power_in", "ground"):
            continue
        if net not in volts:
            return None
        (sup if p["type"] == "power_in" else gnd).append(volts[net])
    if not sup:
        return None
    vals = sup + (gnd or [floor])
    return min(vals), max(vals)


# A connector pin's interface level when the board states none: USB 2.0
# D+/D- signal at most 3.6 V, Type-C CC at most 5.5 V (VCONN). Matched on the
# pin's own name (the extraction's, else the footprint's pinfunction) on a
# connector whose value, footprint or MPN says USB (#1006).
USB_LEVELS = ((re.compile(r"^(?:USB_?)?D(?:[PN]|[+-])\d?$|^D\d[+-]$"), 3.6,
               "USB 2.0 D+/D- signal level"),
              (re.compile(r"^CC\d?$"), 5.5, "USB Type-C CC level"))
USB_RE = re.compile(r"USB|TYPE.?C", re.I)


def connector_levels(board: list[dict], exts: dict,
                     constraints: dict | None) -> dict:
    """{(ref, pad): (lo, hi, why)} for connector pins whose level is
    declared: constraints interface_levels[] ({ref, max, min?, pins?}), then
    a USB connector's D+/D- and CC pins by name. A pin with neither stays
    unknown: the far side is off the board (#1006)."""
    out: dict[tuple, tuple] = {}
    by_ref = {b["ref"]: b for b in board}
    for e in (constraints or {}).get("interface_levels") or []:
        b = by_ref.get(e.get("ref"))
        hi = e.get("max")
        if b is None or not isinstance(hi, (int, float)):
            continue
        lo = e.get("min", 0.0)
        pins = e.get("pins")
        for pad in b["pads"]:
            if pins is None or pad in pins:
                out[(b["ref"], pad)] = (float(lo), float(hi),
                                        f"constraints interface_levels {b['ref']}")
    for b in board:
        if not INPUT_CONN_RE.match(b["ref"]):
            continue
        ext = exts.get(b["lcsc"]) or {}
        if not USB_RE.search(" ".join(str(x) for x in (
                b.get("value"), b.get("footprint"), ext.get("mpn")))):
            continue
        names = {p["pin"]: p["name"] for p in ext.get("pinout") or []}
        for pad in b["pads"]:
            name = str(names.get(pad) or (b.get("fns") or {}).get(pad) or "")
            name = name.upper().replace(" ", "")
            for rx, hi, why in USB_LEVELS:
                if rx.match(name):
                    out.setdefault((b["ref"], pad), (0.0, hi, f"{b['ref']} {why}"))
    return out


def _out_amps(ext: dict | None, pad: str) -> tuple[float, str] | None:
    """The current a driver's pin is rated to source or sink: its abs-max
    current row, else its recommended one."""
    rows = part_ratings(ext).get(pad, {}) if ext else {}
    for level in ("abs", "rec"):
        for r in rows.get(("current", level), []):
            if isinstance(r["hi"], float) and r["hi"] != 0:
                return abs(r["hi"]), r["src"]
    return None


def unstated_net_bounds(board: list[dict], exts: dict, bom_by_lcsc: dict,
                        volts: dict, levels: dict | None = None,
                        not_judged: list | None = None) -> tuple[dict, dict]:
    """(bounds, clamp_power) for nets with no stated voltage.

    bounds[net] = (lo, hi, why, makers): the voltage the net can reach, and
    the refs whose own pins made that bound (they are not judged against
    it). A net is bounded only when everything on it is a resistor, a
    capacitor, an input pin, a clamp or a driver - an output pin of a part
    whose supplies are all known (#869), never a connector's. Each resistor
    to a known or bounded net contributes that net's span, each to any other
    net the board's rails; a driver adds its rail to the feeds' hull. A
    push-pull output takes only the feeds from nets that are stated or
    themselves driven: one to any other net (a series resistor to an LED,
    hysteresis into a divider) is held at its rail, but a pull-up past its
    rail reaches the net, as it would through an open-drain pin typed
    `output` (#1006). A driver is a maker of the bound, and so not judged
    against it, unless a feed from a stated or driven net pulls the net
    past its own rail: a soft hull of the board's rails does not count. A zener/TVS clamp then narrows it from its other side: A
    in K - Vz .. K + a diode drop, K in A - a diode drop .. A + Vz (#868).
    Bounded nets feed each other, so the passes repeat until nothing moves.
    clamp_power[ref] = (watts the clamp can dissipate, its Pd): a clamp is
    judged by the power it can carry, not against the bound it makes. On a
    driven net that power is the current the driver's pin is rated for
    times what the clamp holds, when the driver's rail reaches past the
    clamp; a rail inside the clamp's window puts nothing through it. With no
    current row it is not judged (noted in not_judged) (#1006).
    levels: connector_levels(); a connector pin with a declared level acts
    as a push-pull driver of that level."""
    levels = levels or {}
    floor = min([0.0, *volts.values()])
    ceil = max([0.0, *volts.values()])
    on: dict[str, list] = {}
    for b in board:
        for pad, net in b["pads"].items():
            if net not in volts:
                on.setdefault(net, []).append((b, pad))
    clamps = {b["ref"]: cr for b in board
              if (cr := clamp_row(b, exts.get(b["lcsc"]),
                                  bom_by_lcsc.get(b["lcsc"])))}
    info: dict[str, tuple] = {}
    for net, pins in on.items():
        feeds, cl, drv, ok = [], [], [], True
        for b, pad in pins:
            ref, ext = b["ref"], exts.get(b["lcsc"])
            if (ref, pad) in levels:
                lo_l, hi_l, _ = levels[(ref, pad)]
                drv.append((ref, "output", (lo_l, hi_l), pad, None))
                continue
            if ref in clamps:
                cl.append(b)
                continue
            if re.match(r"^R\d", ref) and len(b["pads"]) == 2:
                other = next((n for q, n in b["pads"].items() if q != pad), None)
                r = _ohms(b.get("value"))
                if other is None or other == net or r is None or r <= 0:
                    ok = False
                    break
                feeds.append((r, other))
                continue
            if re.match(r"^C\d", ref):
                continue
            ptype = next((p["type"] for p in (ext or {}).get("pinout") or []
                          if p["pin"] == pad), None)
            if ptype in ("input", "passive"):
                continue
            # a connector's pins are driven from off the board: its own
            # supply pins say nothing about what arrives on the others
            rail = (_supply_rail(b, ext, volts, floor) if ptype in OUT_TYPES
                    and not INPUT_CONN_RE.match(ref) else None)
            if rail is None:
                ok = False
                break
            drv.append((ref, ptype, rail, pad, ext))
        # a connector's declared level and a part on the board whose rail
        # reaches past it (a PD controller's CC pin on a VBUS-fed supply):
        # its rail says what it is fed from, not what it drives the line
        # to, so the net is left unbounded rather than given that rail
        lv = [d for d in drv if (d[0], d[3]) in levels]
        past = sorted({f"{d[0]}.{d[3]}" for d in drv if d not in lv and any(
            d[2][0] < l[2][0] - EPS or d[2][1] > l[2][1] + EPS for l in lv)})
        if ok and lv and past:
            ok = False
            if not_judged is not None:
                not_judged.append(
                    f"{net}: {', '.join(past)} sits on a rail past "
                    f"{lv[0][0]}.{lv[0][3]}'s {_fmt(lv[0][2][1])} V level "
                    f"({levels[(lv[0][0], lv[0][3])][2]}), so the net was "
                    "not bounded")
        # a bare hull of the board's rails proves nothing
        if ok and ((feeds and cl) or drv):
            info[net] = (feeds, cl, drv)

    bounds: dict[str, tuple] = {}

    def span(n):
        if n in volts:
            return volts[n], volts[n]
        return bounds[n][:2] if n in bounds else None

    def other_pad(c, pad):
        return next(n for q, n in c["pads"].items() if q != pad)

    for _ in range(len(info) + 1):
        moved = False
        for net, (feeds, cl, drv) in info.items():
            pts = [span(o) or (floor, ceil) for _, o in feeds]
            # held: a feed from a stated or itself driven net; one bounded
            # only by resistors is a soft hull of the rails, not a source
            held = [s for _, o in feeds if (s := span(o)) and (
                o in volts or (o in info and info[o][2]))]
            if drv:
                use = [d[2] for d in drv]
                if any(d[1] == "output" for d in drv):
                    # a push-pull pin holds the net to its rail, but a
                    # pull-up to a held net past it says the pin is open
                    # drain in fact, typed `output` (#1006)
                    known = held
                else:
                    known = pts
                use += known
                why = "driven by " + ", ".join(sorted({d[0] for d in drv})) \
                    + " from its rail"
                lv = sorted({levels[(d[0], d[3])][2] for d in drv
                             if (d[0], d[3]) in levels})
                if lv:
                    why += " (" + "; ".join(lv) + ")"
                if known:
                    why += " and feed resistors"
            else:
                use, why = pts, "feed resistors"
            lo, hi = min(u[0] for u in use), max(u[1] for u in use)
            makers = set()
            for c in cl:
                kpin, vz = clamps[c["ref"]]
                knet = c["pads"][kpin]
                anet = other_pad(c, kpin)
                if knet == anet:
                    continue
                s = span(anet if net == knet else knet)
                if s is None:
                    continue
                if net == anet:
                    lo, hi = max(lo, s[0] - vz), min(hi, s[1] + DIODE_DROP_V)
                else:
                    lo, hi = max(lo, s[0] - DIODE_DROP_V), min(hi, s[1] + vz)
                why += f", clamped by {c['ref']}"
                makers.add(c["ref"])
            if lo > hi + EPS:
                continue
            # a driver made the bound unless a held feed pulls the net past
            # its rail: then its own pin is judged at that pull (#1006)
            hard = [d[2] for d in drv] + held
            hlo = max(lo, min(u[0] for u in hard)) if hard else lo
            hhi = min(hi, max(u[1] for u in hard)) if hard else hi
            makers |= {d[0] for d in drv if d[2][0] - EPS <= hlo
                       and hhi <= d[2][1] + EPS}
            new = (lo, hi, why, frozenset(makers))
            old = bounds.get(net)
            if old is None or abs(old[0] - lo) > EPS or abs(old[1] - hi) > EPS \
                    or old[2:] != new[2:]:
                bounds[net] = new
                moved = True
        if not moved:
            break

    clamp_power: dict[str, tuple] = {}
    for net, (feeds, cl, drv) in info.items():
        if net not in bounds:
            continue
        for c in cl:  # the clamp's own evidence excludes itself
            kpin, vz = clamps[c["ref"]]
            pd = _watts(_attr(bom_by_lcsc.get(c["lcsc"]), PD_ATTR_RE))
            far = c["pads"][kpin]
            if far == net:
                far = other_pad(c, kpin)
            s = span(far) if far != net else None
            if s is None:
                continue
            if drv:
                # what the drivers force through the clamp: K on the net
                # conducts below A - a drop and above A + Vz; A on it, below
                # K - Vz and above K + a drop
                k_side = c["pads"][kpin] == net
                lo_ok, hi_ok = ((s[1] - DIODE_DROP_V, s[0] + vz) if k_side
                                else (s[1] - vz, s[0] + DIODE_DROP_V))
                watts, why, src = 0.0, [], []
                for d in drv:
                    r_lo, r_hi = d[2]
                    if r_lo >= lo_ok - EPS and r_hi <= hi_ok + EPS:
                        continue
                    amps = _out_amps(d[4], d[3])
                    if amps is None:
                        why.append(f"{d[0]}.{d[3]}")
                        continue
                    held = vz if (r_hi > hi_ok + EPS) == k_side else DIODE_DROP_V
                    watts += amps[0] * held
                    src.append(f"{d[0]}.{d[3]} at its rated "
                               f"{amps[0] * 1000:.3g} mA [{amps[1]}]")
                if why:
                    if not_judged is not None:
                        not_judged.append(
                            f"{c['ref']}: a clamp on {net} that "
                            f"{', '.join(why)} drives past its "
                            f"{_fmt(vz)} V, with no current rating on that "
                            "pin, so its power was not judged")
                    continue
                if watts == 0.0 or pd is not None:
                    clamp_power[c["ref"]] = (
                        watts, pd if pd is not None else float("inf"),
                        "from " + ", ".join(src) if src else "from its drivers")
                continue
            if pd is None:
                continue
            amps = 0.0
            for r, o in feeds:
                f = span(o) or (floor, ceil)
                amps += max(abs(x - y) for x in s for y in f) / r
            clamp_power[c["ref"]] = (amps * vz, pd, "from its feed")
    return bounds, clamp_power


def decoupling_only(board: list[dict], net: str, ref: str) -> bool:
    """True when net holds pads of `ref` and at least one 2-pad capacitor
    whose other pad is a ground net, and nothing else (#843)."""
    caps = 0
    for b in board:
        if net not in b["pads"].values() or b["ref"] == ref:
            continue
        others = set(b["pads"].values()) - {net}
        if not (re.match(r"^C\d", b["ref"]) and len(b["pads"]) == 2
                and len(others) == 1 and is_ground(next(iter(others)))):
            return False
        caps += 1
    return caps > 0


def run_check(pcb: Path, parts_dir: Path, constraints: dict | None,
              power_tree: Path | None,
              cons_dir: Path | None = None,
              trusted: set[str] | None = None,
              extractions: Path | None = None) -> tuple[list[dict], dict]:
    """trusted: refdes whose rating a person checked and recorded verified in
    the ledger (#429) - their 'unrated' warnings are dropped (a real
    over-rating error still fires). extractions: a further folder of
    extractions, read after the board's parts/ and the library (#1339)."""
    pj = parts_dir / "parts.json"
    bom = checklib.load_json(pj, "parts.json").get("parts", []) \
        if pj.is_file() else []
    # footprints that are not parts - excluded from the BOM (printed RF
    # structures, logos, test coupons) or classed board_feature - have no
    # rating to check (#429)
    feats = _board_features(bom)
    board = [b for b in load_parts_on_board(pcb)
             if b["in_bom"] and b["ref"] not in feats]
    trusted = set(trusted or ())
    bom_by_lcsc = {str(p.get("lcsc") or "").strip(): p for p in bom
                   if p.get("lcsc")}
    by_mpn = key_by_mpn(board, bom, parts_dir, bom_by_lcsc, extractions)
    exts: dict[str, dict] = {}
    for lcsc in {b["lcsc"] for b in board if b["lcsc"]}:
        if (ext := load_extraction(parts_dir, lcsc, extractions)) is not None:
            exts[lcsc] = ext
    # pins as the copper maps them (#843), before any pin is read
    by_position, position_not_used = {}, {}
    for b in board:
        moved, why = pin_map(b, exts.get(b["lcsc"]))
        if moved:
            apply_pin_map(b, moved)
            by_position[b["ref"]] = b["pin_map"]
        elif why:
            position_not_used[b["ref"]] = why

    nets = {n for b in board for n in b["pads"].values()}
    volts: dict[str, float] = {}
    vsrc: dict[str, str] = {}

    def setv(net, v, src):
        if net in nets and net not in volts and v is not None:
            volts[net], vsrc[net] = float(v), src

    for e in (constraints or {}).get("voltages") or []:
        setv(e.get("net"), e.get("voltage"), "constraints")
    for net, v in power_tree_volts(power_tree, nets).items():
        setv(net, v, "power_tree")
    for net in nets:
        setv(net, volts_from_name(net), "net name")
    # after the name: the board says what a rail is, the part is judged
    # against it (#721). The MPN only fills in a rail nothing else names.
    for b in board:
        ext = exts.get(b["lcsc"])
        v = reg_out_volts(str(ext.get("mpn") or "")) if ext else None
        if v is None:
            continue
        for pin in reg_out_pins(b, ext):
            setv(b["pads"][pin], v, f"regulator {b['ref']}")
    for b in board:
        for p in (exts.get(b["lcsc"]) or {}).get("pinout") or []:
            net = b["pads"].get(p["pin"])
            if p["type"] == "power_out" and p.get("internal_v") is not None \
                    and net and decoupling_only(board, net, b["ref"]):
                setv(net, p["internal_v"], f"internal regulator {b['ref']}."
                     f"{p['pin']} {p['name']}, datasheet value, decoupling "
                     "only on the net")
    # pull-ups: a 2-pad resistor from an unknown net to a known rail. A net
    # that also has a resistor to ground or to another unknown net is a
    # divider or a series path - its voltage stays unknown.
    pulled: dict[str, float] = {}
    spoiled: set[str] = set()
    for b in board:
        if re.match(r"^R\d", b["ref"]) and len(set(b["pads"].values())) == 2:
            a, c = sorted(set(b["pads"].values()))
            for x, y in ((a, c), (c, a)):
                if x in volts:
                    continue
                if volts.get(y, 0) > 0:
                    pulled[x] = max(pulled.get(x, 0.0), volts[y])
                else:
                    spoiled.add(x)
    for net, v in pulled.items():
        if net not in spoiled:
            setv(net, v, "pull-up")
    currents = {e.get("net"): float(e["current_a"])
                for e in (constraints or {}).get("power") or []
                if e.get("current_a") is not None}

    out: list[dict] = []
    unknown_net_pins = 0
    unknown_rails: dict[str, list[str]] = {}
    checked_pins = 0

    def emit(kind, sev, ref, net, msg, **extra):
        if ref in by_mpn:
            extra.setdefault("part_key", f"MPN {by_mpn[ref]}")
        pin = str(extra.get("pin") or "")
        back = {v: k for k, v in by_position.get(ref, {}).items()}
        if pin.startswith(f"{ref}.") and (pad := back.get(pin[len(ref) + 1:])):
            # pins are the datasheet's here (#843): name the copper too
            extra["board_pad"] = pad
            msg += (f" (datasheet pin {pin[len(ref) + 1:]} is the "
                    f"footprint's pad {pad}, by the land drawing)")
        out.append(violation(SCRIPT, sev, None, None, net, [ref], msg, SCRIPT,
                             kind=kind, **extra))

    unrated_passives: list[str] = []
    covered_pins: list[str] = []
    checked_by_ref: dict[str, int] = {}
    rated_by_ref: dict[str, set[str]] = {}

    def compared(ref: str, tier: str) -> None:
        """A rating of ref held a number from this tier against known volts."""
        checked_by_ref[ref] = checked_by_ref.get(ref, 0) + 1
        rated_by_ref.setdefault(ref, set()).add(tier)

    netfns = net_pinfunctions(pcb)
    for ref, moved in by_position.items():  # into the extraction's pins too
        if ref in netfns:
            netfns[ref] = {moved.get(k, k): v for k, v in netfns[ref].items()}
    fb_ctx = {"board": board, "bom_by_lcsc": bom_by_lcsc, "volts": volts,
              "vsrc": vsrc, "blocks": block_vouts(constraints),
              "vrefs": load_vrefs(), "not_judged": []}
    diodes = {b["ref"]: d for b in board
              if (d := diode_reverse(b, exts.get(b["lcsc"]),
                                     bom_by_lcsc.get(b["lcsc"]), netfns))}
    clamp_not_judged: list[str] = []
    bounds, clamp_power = unstated_net_bounds(
        board, exts, bom_by_lcsc, volts,
        connector_levels(board, exts, constraints), clamp_not_judged)
    power_judged: set[str] = set()
    for b in sorted(board, key=lambda x: x["ref"]):
        ref, pads = b["ref"], b["pads"]
        if not pads:
            continue
        ext = exts.get(b["lcsc"])
        if ext is None and is_fuse(b, bom_by_lcsc.get(b["lcsc"])):
            continue  # rated rail-to-ground, not across: fuse_findings
        dio = diodes.get(ref)
        if ext is None and dio:
            vk, va = volts.get(pads[dio["k"]]), volts.get(pads[dio["a"]])
            if vk is not None and va is not None:
                compared(ref, "distributor" if dio["tier"] == "datasheet"
                         else dio["tier"])
                vrev = _diode_reverse_v(dio, vk, va)
                if vrev > dio["vr"] + EPS:
                    emit("rating_reverse_polarity", "error", ref,
                         pads[dio["k"]],
                         f"{ref} is reversed by {_fmt(vrev)} V (cathode "
                         f"{pads[dio['k']]}, anode {pads[dio['a']]}), over "
                         f"its {_fmt(dio['vr'])} V reverse rating "
                         f"[{dio['src']}; {dio['how']}]",
                         pin=f"{ref}.{dio['k']}", net_v=vrev,
                         limit_v=dio["vr"], rating=dio["src"])
            continue
        if ext is None:
            bom = bom_by_lcsc.get(b["lcsc"])
            power_ic = bool(bom and bom.get("attributes") and power_ic_by_family(
                ref, str(bom.get("mpn") or "")))
            if (power_ic_findings(b, bom, volts, vsrc, netfns, emit)
                    if power_ic else regulator_findings(
                        b, bom, volts, vsrc, netfns, emit, fb_ctx)):
                compared(ref, "distributor")
                continue
            why = power_ic_why_unrated(b, bom, volts, vsrc, netfns) if power_ic \
                else None
            if tier := _check_unextracted(b, bom_by_lcsc.get(b["lcsc"]), volts,
                                          emit, unrated_passives, why):
                compared(ref, tier)
            continue
        extracted_output_findings(b, ext, bom_by_lcsc.get(b["lcsc"]), volts,
                                  vsrc, emit, fb_ctx)
        ratings = part_ratings(ext)
        name_of = {p["pin"]: p for p in ext.get("pinout") or []}
        pair_refs = {r["ref"] for rows in ratings.values()
                     for lst in rows.values() for r in lst if r["ref"]}
        idx = pin_index(ext.get("pinout") or [])

        def ref_volts(name):
            hits = idx.get(str(name).upper()) or []
            if not hits:  # a name aliases() drops: a passive's "2" (rating_fill)
                hits = [q for q in ext.get("pinout") or []
                        if str(name) in (q["pin"], q["name"])]
            if not hits and str(name).upper() in ("VS", "V+"):
                hits = idx.get("V+") or []
            if not hits and str(name).upper().startswith("V"):
                hits = idx.get(str(name).upper()[1:]) or []
            if not hits:  # VDDIOX -> VDDIO*, VDD*: prefix match
                stem = str(name).upper().rstrip("X")
                hits = [p for a, ps in idx.items() if a.startswith(stem)
                        for p in ps]
            vs = [volts[pads[h["pin"]]] for h in hits
                  if h["pin"] in pads and pads[h["pin"]] in volts]
            return max(vs) if vs else None

        gnd_all = {pads[q["pin"]] for q in ext.get("pinout") or []
                   if q["type"] == "ground" and q["pin"] in pads}
        # a rating that names no reference is measured from the part's own
        # ground: 0 V, unless all its ground pins sit on one negative rail
        # (#1006); a part with a 0 V ground too keeps 0 V
        gnd_nets = sorted(gnd_all) if gnd_all and all(
            volts.get(n, 0.0) < -EPS for n in gnd_all) and len(
            {volts[n] for n in gnd_all}) == 1 else []
        gnd_v = volts[gnd_nets[0]] if gnd_nets else 0.0
        gnd_rel = f" vs its ground {gnd_nets[0]}" if gnd_nets else ""

        def resolve(bound):
            if bound is None or isinstance(bound, float):
                return bound, None
            v = ref_volts(bound[0])
            return (None if v is None else v + bound[1]), bound[0]

        def self_driven(p):
            """A pin the part itself drives: an output, a power output, or a
            pin the extraction marks `self_driven` (a reference or
            charge-pump node)."""
            return p["type"] in ("output", "power_out") or bool(p.get("self_driven"))

        def supply_rows():
            """The part's supply rating: the abs-max rows (else recommended)
            on its power_in pins, as (hi, ref name, source). Only a stated max
            counts."""
            got = {}
            for level in ("abs", "rec"):
                for q in ext.get("pinout") or []:
                    if q["type"] != "power_in":
                        continue
                    for r in ratings.get(q["pin"], {}).get(("voltage", level), []):
                        hi, _ = resolve(r["hi"]) if not isinstance(r["hi"], tuple) \
                            else (None, None)
                        if hi is not None:
                            got[(hi, r["ref"], r["src"])] = None
                if got:
                    break
            return [(hi, rf, src) for hi, rf, src in got]

        def bootstrap_net(ref_, pin_, net_):
            """A bootstrap pin whose net reaches only one two-pad capacitor,
            whose other pad is on the net of the part's own switch pin."""
            on = [(b2["ref"], pad) for b2 in board for pad, n2 in b2["pads"].items()
                  if n2 == net_ and (b2["ref"], pad) != (ref_, pin_)]
            if len(on) != 1 or not on[0][0].startswith("C"):
                return False
            cap = next(b2 for b2 in board if b2["ref"] == on[0][0])
            if len(cap["pads"]) != 2:
                return False
            other = next(n2 for pad, n2 in cap["pads"].items() if pad != on[0][1])
            return any(n2 == other and pad != pin_
                       and str((name_of.get(pad) or {}).get("name", "")).upper()
                       .startswith(("OUT", "SW", "LX", "PH"))
                       for pad, n2 in pads.items())

        unrated: list[str] = []
        covered_by_rule: list[str] = []
        if not name_of and pads:
            unrated.append("the extraction has no pinout")
        for pin, net in sorted(pads.items()):
            p = name_of.get(pin)
            if p is None:
                # a ground pad (the usual EP) stresses nothing to compare
                if name_of and not is_ground(net):
                    unrated.append(f"{pin}: on {net}, not in the extraction's "
                                   "pinout")
                continue
            if p["type"] == "nc":
                continue
            label = f"{ref}.{pin} ({p['name']})"
            v = volts.get(net)
            if p["type"] == "ground":
                if v is not None and v > 0.5 and not is_ground(net):
                    emit("rating_reverse_polarity", "error", ref, net,
                         f"{label} is a ground pin on {net} = {_fmt(v)} V "
                         f"({vsrc[net]})", pin=f"{ref}.{pin}", net_v=v)
                continue
            rows = ratings.get(pin, {})
            if p["type"] == "power_out" and net in currents:
                for r in rows.get(("current", "abs"), []):
                    hi, _ = resolve(r["hi"])
                    if hi is not None and currents[net] > abs(hi) + EPS:
                        emit("rating_over_current", "warning", ref, net,
                             f"{label} feeds {net} at {currents[net]} A "
                             f"(constraints power[]), over its rated "
                             f"{_fmt(abs(hi))} A [{r['src']}]",
                             pin=f"{ref}.{pin}", limit_a=abs(hi))
            vabs = rows.get(("voltage", "abs"), [])
            vrec = rows.get(("voltage", "rec"), [])
            if not vabs and not vrec and p["name"] in pair_refs:
                continue  # the other side of a pair rating (a diode's A)
            if not vabs and not vrec:
                if self_driven(p):
                    sup = supply_rows()
                    if sup:
                        # a pin the part drives itself (#835): its supply's
                        # rating covers it; only a net from outside the part
                        # is judged, against that supply rating
                        if v is not None and vsrc[net] != "pull-up":
                            for hi, hi_name, src in sup:
                                vr = ref_volts(hi_name) if hi_name else gnd_v
                                if vr is None:
                                    continue
                                if v - vr > hi + EPS:
                                    emit("rating_over_voltage", "error", ref, net,
                                         f"{label} is a pin the part drives itself, "
                                         f"covered by its supply rating (rule, not a "
                                         f"datasheet row on this pin), and an outside "
                                         f"net holds {net} = {_fmt(v - vr)} V "
                                         f"({vsrc[net]}), over that supply's "
                                         f"{_fmt(hi)} V [{src}]",
                                         pin=f"{ref}.{pin}", net_v=v, limit_v=hi,
                                         rating=src, rule="driven-pin-supply-cover")
                                    break
                            else:
                                checked_pins += 1
                                compared(ref, "datasheet")
                        covered_by_rule.append(f"{ref}.{pin} {p['name']}: driven by the "
                                               "part, covered by its supply rating")
                        continue
                if p["name"].upper().startswith(("BS", "BOOT")) \
                        and bootstrap_net(ref, pin, net):
                    covered_by_rule.append(f"{ref}.{pin} {p['name']}: bootstrap net "
                                           "of only the cap and the part's own "
                                           "switch pin")
                    continue
                unrated.append(f"{pin} {p['name']}: no rating names it")
                continue
            if v is not None and vsrc[net] == "pull-up" \
                    and p["type"] == "power_in":
                v = None  # a series resistor into a supply pin: drop unknown
            if v is None and net in bounds:
                lo_n, hi_n, bwhy, makers = bounds[net]
                if ref in makers:
                    # the bound's own evidence is not judged against it: a
                    # clamp by the power its feed can force through it, a
                    # driver by its supply rating
                    if ref in clamp_power and ref not in power_judged:
                        power_judged.add(ref)
                        if clamp_power[ref][0] > clamp_power[ref][1]:
                            emit("rating_over_current", "warning", ref, net,
                                 f"{label} is a clamp that can carry "
                                 f"{clamp_power[ref][0]:.2g} W {clamp_power[ref][2]}, "
                                 f"over its {clamp_power[ref][1]:.2g} W "
                                 "power rating", pin=f"{ref}.{pin}")
                        checked_pins += 1
                        compared(ref, "datasheet")
                    elif ref in clamp_power:
                        checked_pins += 1
                    else:
                        covered_by_rule.append(
                            f"{ref}.{pin} {p['name']}: makes the bound on "
                            f"{net} ({bwhy}) itself")
                    continue
                judged = True
                for r in vabs:
                    vr = ref_volts(r["ref"]) if r["ref"] else gnd_v
                    lo, _ = resolve(r["lo"])
                    hi, _ = resolve(r["hi"])
                    if vr is None or isinstance(r["lo"], tuple) \
                            or isinstance(r["hi"], tuple):
                        judged = False
                        continue
                    rel = f" vs {r['ref']}" if r["ref"] else gnd_rel
                    span = f"{_fmt(lo_n)}..{_fmt(hi_n)} V ({bwhy})"
                    base = dict(pin=f"{ref}.{pin}", rating=r["src"],
                                bound_v=[lo_n, hi_n])
                    if hi is not None and hi_n - vr > hi + EPS:
                        emit("rating_over_voltage", "warning", ref, net,
                             f"{label} on {net}, which the board states no "
                             f"voltage for but which lies in {span}, reaches "
                             f"{_fmt(hi_n - vr)} V{rel}, over its absolute "
                             f"max {_fmt(hi)} V [{r['src']}]", limit_v=hi, **base)
                    elif lo is not None and lo_n - vr < lo - EPS:
                        emit("rating_reverse_polarity", "warning", ref, net,
                             f"{label} on {net}, which the board states no "
                             f"voltage for but which lies in {span}, reaches "
                             f"{_fmt(lo_n - vr)} V{rel}, below its absolute "
                             f"min {_fmt(lo)} V [{r['src']}]", limit_v=lo, **base)
                if judged and vabs:
                    checked_pins += 1
                    compared(ref, "datasheet")
                else:
                    unknown_net_pins += 1
                continue
            if v is None:
                unknown_net_pins += 1
                if p["type"] == "power_in":
                    unknown_rails.setdefault(net, []).append(f"{ref}.{pin}")
                continue
            checked_pins += 1
            compared(ref, "datasheet")
            hit_abs = False
            for level, lst in (("abs", vabs), ("rec", vrec)):
                for r in lst:
                    vp, rel = v - gnd_v, gnd_rel
                    if r["ref"]:
                        vr = ref_volts(r["ref"])
                        if vr is None and ref in clamp_power \
                                and clamp_power[ref][0] <= clamp_power[ref][1]:
                            continue  # a clamp the feed resistors cannot burn
                        if vr is None and ref in clamp_power:
                            emit("rating_over_current", "warning", ref, net,
                                 f"{label} is a clamp that can carry "
                                 f"{clamp_power[ref][0]:.2g} W {clamp_power[ref][2]}, "
                                 f"over its {clamp_power[ref][1]:.2g} W "
                                 "power rating", pin=f"{ref}.{pin}")
                            continue
                        if vr is None:
                            # the ref's net is unknown but lies within the
                            # board's rails: judged when the rating holds at
                            # both extremes (an LED's anode, an emitter)
                            floor = min([0.0, *volts.values()])
                            ceil = max([0.0, *volts.values()])
                            lo, _ = resolve(r["lo"])
                            hi, _ = resolve(r["hi"])
                            if (hi is not None or lo is not None) \
                                    and not (isinstance(r["lo"], tuple)
                                             or isinstance(r["hi"], tuple)) \
                                    and (hi is None or v - floor <= hi + EPS) \
                                    and (lo is None or v - ceil >= lo - EPS):
                                continue
                            unrated.append(
                                f"{pin} {p['name']}: rated against "
                                f"{r['ref']}, whose net has no known voltage"
                                f" (at the board's rails, {_fmt(floor)}.."
                                f"{_fmt(ceil)} V, it could break the rating)")
                            continue
                        vp, rel = v - vr, f" vs {r['ref']}"
                    lo, lo_name = resolve(r["lo"])
                    hi, hi_name = resolve(r["hi"])
                    for nm, bnd in ((lo_name, lo), (hi_name, hi)):
                        if nm and bnd is None:
                            unrated.append(f"{pin} {p['name']}: bound is "
                                           f"relative to {nm}, whose net has "
                                           "no known voltage")
                    base = dict(pin=f"{ref}.{pin}", net_v=v, rating=r["src"])
                    # a volt inferred through a pull-up may be a deliberate
                    # series limit into a clamped pin: surfaced, not failed
                    sev = "warning" if vsrc[net] == "pull-up" else "error"
                    if level == "abs" and hi is not None and vp > hi + EPS:
                        hit_abs = True
                        why = (f" - input above supply: {hi_name} is "
                               f"{_fmt(hi - r['hi'][1])} V" if hi_name else "")
                        emit("rating_over_voltage", sev, ref, net,
                             f"{label} on {net} = {_fmt(vp)} V{rel} "
                             f"({vsrc[net]}) exceeds its absolute max "
                             f"{_fmt(hi)} V{why} [{r['src']}]", limit_v=hi,
                             **base)
                    elif level == "abs" and lo is not None and vp < lo - EPS:
                        hit_abs = True
                        emit("rating_reverse_polarity", sev, ref, net,
                             f"{label} on {net} = {_fmt(vp)} V{rel} "
                             f"({vsrc[net]}) is below its absolute min "
                             f"{_fmt(lo)} V [{r['src']}]", limit_v=lo, **base)
                    elif level == "rec" and not hit_abs and (
                            (hi is not None and vp > hi + EPS) or
                            (p["type"] == "power_in" and lo is not None
                             and 0 < vp < lo - EPS)):
                        emit("rating_outside_recommended", "warning", ref, net,
                             f"{label} on {net} = {_fmt(vp)} V{rel} is outside "
                             f"its recommended {_fmt(lo) if lo is not None else '-'}"
                             f"..{_fmt(hi) if hi is not None else '-'} V "
                             f"[{r['src']}]", **base)
        covered_pins.extend(covered_by_rule)
        if unrated:
            emit("rating_unrated", "warning", ref, None,
                 f"{ref} ({ext.get('mpn')}): {len(unrated)} connected pin(s) "
                 f"could not be checked - {'; '.join(unrated[:6])}"
                 + (" ..." if len(unrated) > 6 else ""),
                 pins=unrated)
    # a 2-net power input plugged in the wrong way round: what each diode
    # it reaches is asked to block (a design that says "the LED blocks
    # reverse current" is claiming exactly this). A fault case, so a warning.
    reversed_inputs = []
    for conn, rail in input_connectors(board, volts):
        rev = reversed_input_volts(board, volts, vsrc, rail)
        reversed_inputs.append({"connector": conn["ref"], "rail": rail,
                                "rail_v": -volts[rail]})
        for ref, dio in sorted(diodes.items()):
            pads = next(b["pads"] for b in board if b["ref"] == ref)
            vk, va = rev.get(pads[dio["k"]]), rev.get(pads[dio["a"]])
            if vk is None or va is None:
                continue
            vrev = _diode_reverse_v(dio, vk, va)
            if vrev <= dio["vr"] + EPS:
                continue
            emit("rating_reverse_polarity", "warning", ref, rail,
                 f"{conn['ref']} plugged in reversed puts {_fmt(vrev)} V "
                 f"across {ref} the wrong way (cathode {pads[dio['k']]}, "
                 f"anode {pads[dio['a']]}; {rail} = {_fmt(volts[rail])} V, "
                 "no drop in the series parts while it blocks), over its "
                 f"{_fmt(dio['vr'])} V reverse rating [{dio['src']}; "
                 f"{dio['how']}]", pin=f"{ref}.{dio['k']}", net_v=vrev,
                 limit_v=dio["vr"], rating=dio["src"],
                 scenario=f"{conn['ref']} reversed")
    diode_forward_findings(board, bom_by_lcsc, exts, netfns, volts, vsrc,
                           emit)
    clamped = clamp_findings(board, exts, bom_by_lcsc, netfns, volts, vsrc,
                             constraints, diodes, emit)
    amb = ambient(constraints, cons_dir)
    before_fuses = dict(checked_by_ref)
    fuse_facts = fuse_findings(board, bom_by_lcsc, volts, vsrc, currents,
                               amb, emit, checked_by_ref, unrated_passives)
    for ref, n in checked_by_ref.items():
        if n > before_fuses.get(ref, 0):    # a fuse is rated off its parts.json row
            rated_by_ref.setdefault(ref, set()).add("distributor")
    temp_facts = temperature_findings(board, bom_by_lcsc, exts, amb, emit)
    unrated_passives = [r for r in unrated_passives if r not in trusted]
    out = [v for v in out if not (v.get("kind") == "rating_unrated"
                                  and set(v.get("refs") or []) <= trusted
                                  and v.get("refs"))]
    lcsc_of = {b["ref"]: b["lcsc"] for b in board}
    no_attribute_findings(board, bom_by_lcsc, exts, trusted, emit, out)
    bare = [r for r in unrated_passives
            if not (bom_by_lcsc.get(lcsc_of.get(r)) or {}).get("attributes")]
    if unrated_passives:
        emit("rating_unrated", "warning", None, None,
             f"{len(unrated_passives)} two-terminal part(s) have no extraction "
             "and no voltage-rated attribute in parts.json: "
             + ", ".join(unrated_passives)
             + (f" ({', '.join(bare)}: the parts.json line carries no "
                "attributes - keep parts_search's row whole)" if bare else ""),
             pins=unrated_passives)
        out[-1]["refs"] = sorted(unrated_passives)
    import datasheet_extract
    lcscs = {b["lcsc"] for b in board if b["lcsc"]}
    suspect = datasheet_extract.suspect_datasheets(parts_dir, lcscs)
    for lcsc, why in sorted(suspect.items()):
        refs = sorted(b["ref"] for b in board if b["lcsc"] == lcsc)
        emit("rating_datasheet_suspect", "warning", refs[0] if refs else None,
             None, f"{', '.join(refs)} ({lcsc}): the datasheet PDF is suspect "
             f"({why}); what was read from it is not counted as verified, "
             "and a rating that came from the distributor stays listed - "
             "fetch another datasheet or check by eye", pins=refs)
        out[-1]["refs"] = refs
    unassessed = [c for c in datasheet_extract.unassessed_datasheets(
        parts_dir, lcscs) if c not in suspect]
    if unassessed:
        refs = sorted(b["ref"] for b in board if b["lcsc"] in unassessed)
        emit("rating_datasheet_unassessed", "warning", refs[0], None,
             f"{len(unassessed)} part(s) have a datasheet that can't be judged "
             f"here ({', '.join(unassessed[:8])}"
             f"{', ...' if len(unassessed) > 8 else ''}): no PDF on disk, or no "
             "MPN to check it against, so it may be another part's. Run "
             "`datasheet_extract --fetch <ws>`, which downloads and judges it",
             pins=refs)
        out[-1]["refs"] = refs
    # what an extraction was read from may be another part's PDF, off this
    # board: it holds that fact back without being this board's to warn about
    read_from = {c: extraction_pdf(e, c) for c, e in exts.items()}
    held_back = {**suspect, **datasheet_extract.suspect_datasheets(
        parts_dir, set(read_from.values()) - lcscs)}
    lfacts = ledger_facts(board, exts, out, checked_by_ref, bom_by_lcsc,
                          held_back, rated_by_ref)
    unread = sorted(f["subject"] for f in lfacts if f["kind"] == "rating"
                    and f["status"] == "listed"
                    and f["subject"] not in trusted)
    if unread:
        emit("rating_no_datasheet_read", "warning", unread[0], None,
             f"{len(unread)} part(s) were rated from a distributor's attribute,"
             f" a part number's voltage code or family or a package table,"
             f" with no datasheet read ({', '.join(unread[:8])}"
             f"{', ...' if len(unread) > 8 else ''}). This doesn't block "
             "release. To make them verified: run `datasheet_extract --fetch "
             "<ws>` and extract each PDF. A part with no number at all: "
             "`parts_enrich`", pins=unread)
        out[-1]["refs"] = unread
    listing_compared, listing_skipped = listing_findings(
        board, exts, bom_by_lcsc, read_from, held_back, rated_by_ref, trusted,
        emit, out)
    nothing, unjudged = (None, None) if checked_by_ref else \
        why_nothing_judged(board, exts, out, volts, bom_by_lcsc)
    facts = {"parts_on_board": len(board), "extractions": sorted(exts),
             "keyed_by_mpn": by_mpn,
             "net_volts": {n: {"v": volts[n], "src": vsrc[n]}
                           for n in sorted(volts)},
             "pins_checked": checked_pins,
             "parts_rated_unextracted": sorted(
                 r for r, t in rated_by_ref.items() if "datasheet" not in t),
             "pins_covered_by_rule": covered_pins,
             "pins_by_position": by_position,
             "pins_by_number_why": position_not_used,
             "reversed_inputs": reversed_inputs,
             "clamped_rails": clamped,
             "pins_on_unknown_nets": unknown_net_pins,
             "not_judged": [
                 f"{net}: no known voltage, so {len(pins)} supply pin(s) were "
                 f"not compared ({', '.join(pins[:6])}"
                 f"{' ...' if len(pins) > 6 else ''})"
                 for net, pins in sorted(unknown_rails.items())]
             + clamp_not_judged + fb_ctx["not_judged"],
             "nothing_to_judge": nothing,
             "parts_not_compared": unjudged,
             "ambient": amb, "fuses": fuse_facts,
             "operating_range": temp_facts,
             "suspect_datasheets": suspect,
             "unassessed_datasheets": unassessed,
             "ledger_facts": lfacts,
             "listing_comparison": listing_compared,
             "listing_not_compared": listing_skipped}
    return out, facts


LIBRARY_DIR = HERE.parent / "reference" / "extractions"


def _ext_dirs(parts_dir: Path, extractions: Path | None) -> list[Path]:
    return [parts_dir, LIBRARY_DIR, *([extractions] if extractions else [])]


def load_extraction(parts_dir: Path, lcsc: str,
                    extractions: Path | None = None) -> dict | None:
    """The part's extraction: the board's own parts/<lcsc>.json, else hwde's
    library entry reference/extractions/<lcsc>.json (read from the maker's
    datasheet, every rating row citing its page - see that folder's
    README.md), else <extractions>/<lcsc>.json (--extractions: a batch
    reading, abs_max_batch.py, #1339), else None. The board's own wins: it
    may be a later revision of the part."""
    for d in _ext_dirs(parts_dir, extractions):
        f = d / f"{lcsc}.json"
        if f.is_file():
            return checklib.load_json(f, f"extraction {f.name}")
    return None


# inductors and ferrite beads are rated by current, and the volts across them
# are their DC drop: no voltage rating is missing, so they are not unrated
NO_VOLTAGE_RATING_RE = re.compile(r"^(?:L|FB)\d")


def _check_unextracted(b: dict, bom: dict | None, volts: dict, emit,
                       unrated_passives: list[str],
                       why: str | None = None) -> str | None:
    """A part with no extraction: a 2-net part's `Voltage Rated` attribute
    is checked across it, else a capacitor's voltage code (cap_code_rating),
    else a resistor's package limit; anything else is an explicit unrated
    finding (two-terminal BOM parts are collected into one finding by the
    caller). The tier the compared rating came from (distributor, part_code,
    package) when a rating was compared with known volts, else None."""
    ref, pads = b["ref"], b["pads"]
    nets = sorted(set(pads.values()))
    rated = bom_volts(bom)
    src, tier = "parts.json", "distributor"
    if rated is None and len(nets) == 2 and (code := cap_code_rating(b, bom)):
        (rated, src), tier = code, "part_code"
    if rated is None and len(nets) == 2 and re.match(r"^R\d", ref):
        rated = engcalc.resistor_umax_v(b.get("footprint"))
        src, tier = _package_src(b.get("footprint")), "package"
    if rated is not None and len(nets) == 2:
        va, vb = volts.get(nets[0]), volts.get(nets[1])
        if va is None or vb is None:
            return None
        if abs(va - vb) > rated + EPS:
            emit("rating_over_voltage", "error", ref, nets[0],
                 f"{ref} is rated {_fmt(rated)} V [{src}] but sits across "
                 f"{nets[0]} = {_fmt(va)} V and {nets[1]} = {_fmt(vb)} V",
                 limit_v=rated, net_v=abs(va - vb), rating=src)
        return tier
    if len(nets) <= 1:
        return None  # a single-net part stresses nothing
    why = why or ("no datasheet extraction" if bom or b["lcsc"] else
                  "no LCSC part on the footprint and no extraction")
    if len(nets) == 2 and NO_VOLTAGE_RATING_RE.match(ref):
        return None
    if bom is not None and len(nets) == 2:
        unrated_passives.append(ref)
        return None
    emit("rating_unrated", "warning", ref, None,
         f"{ref} ({b['lcsc'] or 'no LCSC'}): {why}", pins=[why])
    return None


CAP_REF_RE = re.compile(r"^C\d")


def cap_code_rating(b: dict, bom: dict | None) -> tuple[float, str] | None:
    """(volts, source) for a capacitor with no listed rating, read from the
    maker's voltage code in its MPN (reference/mlcc_voltage_codes.yaml, each
    series cited to the maker's part-numbering guide) or from a voltage
    written in its Value field ("220u/16V"). When both give one and they
    differ, the lower is held and the source names both. None for a part
    that is not a capacitor (a diode's "5V1" is a clamp voltage, not a
    rating) or that states neither."""
    if not CAP_REF_RE.match(b["ref"]):
        return None
    mpn = b.get("mpn") or (bom or {}).get("mpn") or ""
    code = mlcc.mpn_voltage(mpn)
    val = mlcc.value_voltage(b.get("value"))
    msrc = (f"part-number voltage code {code['code']} of {mpn}, "
            f"{code['series']} ({code['cite']})") if code else ""
    vsrc = f"Value field {b.get('value')!r} states {val['text']}" if val else ""
    if code and val and abs(code["v"] - val["v"]) > EPS:
        lo = min(code["v"], val["v"])
        return lo, (f"{vsrc}, but the {msrc} says {_fmt(code['v'])} V: "
                    f"the lower, {_fmt(lo)} V, is held")
    if code:
        return code["v"], msrc + (f"; {vsrc}" if val else "")
    if val:
        return val["v"], vsrc
    return None


def _package_src(footprint: str | None) -> str:
    """Where a resistor's rating comes from when nothing names one for it."""
    return (f"{engcalc.package_code(footprint)} limiting element voltage, "
            "D/CRCW e3 table")


def bom_volts(bom: dict | None) -> float | None:
    """A part's voltage-rated attribute in parts.json (V_ATTR_RE), or None."""
    rated = None
    for a in (bom or {}).get("attributes") or []:
        if V_ATTR_RE.search(str(a.get("name") or "")):
            m = re.search(r"(\d+(?:\.\d+)?)\s*(k?)V", str(a.get("value") or ""))
            if m:
                rated = float(m.group(1)) * (1000 if m.group(2) else 1)
    return rated


REG_WORDS = r"regulator|\bLDO\b|\bbuck\b|\bboost\b|DC.?DC|\bPMIC\b"
REG_TEXT_RE = re.compile(REG_WORDS + r"|\bto\s+\d+(?:\.\d+)?\s*V\b", re.I)
# An extraction describes the whole chip, so "USB to 3.3V UART" is no
# regulator there. Nor do these make the rail on their output pin.
EXT_REG_RE = re.compile(REG_WORDS, re.I)
NOT_REG_RE = re.compile(r"e-?fuse|load.?switch|power.?(?:path|switch)|charger|"
                        r"reference|hot.?swap|supervisor", re.I)
EXT_FIXED_RE = re.compile(r"\bfixed\s+(\d+(?:\.\d+)?)\s*V\b|"
                          r"\b(\d+(?:\.\d+)?)\s*V\s+fixed\b", re.I)


REG_REF_RE = re.compile(r"^(?:U|VR|IC)\d")
REG_ROW_TEXT = ("category", "description", "name", "value", "role")


def is_regulator(b: dict, bom: dict | None) -> bool:
    """A U/VR part its row calls a regulator: a fixed output in the MPN, or
    the row's words ("LDO regulator", "VBUS to 5.1 V")."""
    if not REG_REF_RE.match(b["ref"]):
        return False
    bom = bom or {}
    text = " ".join(str(bom.get(k) or "") for k in REG_ROW_TEXT)
    return reg_out_volts(str(bom.get("mpn") or "")) is not None or \
        bool(REG_TEXT_RE.search(text))


# A regulator's input rating in its parts.json row, plainest name first. The
# rows say it several ways, and "Voltage Rated" means the input on a regulator.
VIN_ATTR_RES = tuple(re.compile(x, re.I) for x in (
    r"input\s*voltage|voltage\s*-\s*input|\bvin\b",
    r"supply\s*voltage|voltage\s*-\s*supply",
    r"operating\s*voltage",
    r"voltage\s*rat(?:ing|ed)|rated\s*voltage"))
VOUT_ATTR_RE = re.compile(r"output\s*voltage|voltage\s*-\s*output", re.I)
NOT_VIN_ATTR_RE = re.compile(r"output|dropout|reference|forward", re.I)
STEP_UP_RE = re.compile(r"boost|step.?up|charge.?pump|sepic|inverting", re.I)
REG_IN_PAD_RE = re.compile(r"^(?:[PA]?VIN\d?|IN\d?|INPUT|VI|[PA]?VCC|[PA]?VDD|"
                           r"VBAT|VBUS|VS|VSUP|VSYS)$")
REG_EN_PAD_RE = re.compile(r"^[~/!N]?(?:EN\d?|ENABLE|SHDN|SDN|SHUTDOWN|CE|RUN|"
                           r"ON|ON/OFF)[#N]?$")
REG_OUT_PAD_RE = re.compile(r"^(?:V?OUT\d?|VO|OUTPUT)$")
# how far a fixed regulator's output may sit from its rail's name: a 5.1 V
# part on +5V is that rail, a 3.3 V part on +5V is not
REG_OUT_TOL = 0.10
# where a rail's volts come from when the board itself says what it is
RAIL_SOURCES = {"constraints", "power_tree", "net name"}


def attr_volts(value: str) -> list[float]:
    """Every voltage in an attribute value: "500mV~5.5V" is [0.5, 5.5]."""
    scale = {"m": 1e-3, "k": 1e3, "K": 1e3, "": 1.0}
    return [float(n) * scale[u] for n, u in
            re.findall(r"(\d+(?:\.\d+)?)\s*([mkK]?)V\b", str(value))]


def reg_vin_rating(bom: dict) -> tuple[float, str] | None:
    """(the top of a regulator's input range, where it was read), or None."""
    atts = [a for a in bom.get("attributes") or []
            if not NOT_VIN_ATTR_RE.search(str(a.get("name") or ""))]
    for rx in VIN_ATTR_RES:
        for a in atts:
            vs = attr_volts(a.get("value"))
            if vs and rx.search(str(a.get("name") or "")):
                return max(vs), f"parts.json {a['name']} = {a['value']}"
    return None


def _fixed_range(v: float, src: str) -> tuple[float, float, str]:
    return v * (1 - REG_OUT_TOL), v * (1 + REG_OUT_TOL), src


def reg_out_range(bom: dict) -> tuple[float, float, str] | None:
    """(lowest, highest, where read) a regulator's row says it can make: a
    range, a list, or one fixed value widened by REG_OUT_TOL. The row's
    attribute, else the fixed output in the MPN."""
    for a in bom.get("attributes") or []:
        vs = attr_volts(a.get("value"))
        if vs and VOUT_ATTR_RE.search(str(a.get("name") or "")):
            src = f"parts.json {a['name']} = {a['value']}"
            break
    else:
        v = reg_out_volts(str(bom.get("mpn") or ""))
        vs, src = ([v], f"MPN {bom.get('mpn')}") if v is not None else ([], "")
    if not vs:
        return None
    if len(vs) == 1:
        return _fixed_range(vs[0], src)
    return min(vs), max(vs), src


def is_step_up(bom: dict, vin: float) -> bool:
    """Its row says it steps up, says it makes more than it can take, or names
    a boost family."""
    text = " ".join(str(bom.get(k) or "") for k in
                    ("category", "description", "name", "value", "role"))
    out = reg_out_range(bom)
    return bool(STEP_UP_RE.search(text)) or \
        bool(_STEP_UP_FAMILY_RE.match(str(bom.get("mpn") or "").upper())) or \
        (out is not None and out[1] > vin * (1 + REG_OUT_TOL))


def power_ic_inputs(b: dict, row: dict, netfns: dict) -> dict:
    """{pad: datasheet name} of a power IC's input pads on this board: by name
    when the board names its pads, else by number, and only on a footprint that
    fits the datasheet's package."""
    ref, pads = b["ref"], b["pads"]
    names = {p: re.sub(r"_\d+$", "", fn.upper()) for p, fn in
             dict(netfns.get(ref) or {}, **b.get("fns") or {}).items()
             if p in pads}
    if names:
        return {p: n for p, n in names.items() if n in row["input"]}
    if set(pads) <= {str(n) for n in range(1, row["pins"] + 1)}:
        return {p: n for n, ps in row["input"].items() for p in ps.split()
                if p in pads}
    return {}


def _no_trusted_volts(net: str, volts: dict, vsrc: dict) -> bool:
    """A pull-up's volts are a guess at the pin, as in regulator_findings."""
    return volts.get(net) is None or vsrc.get(net) == "pull-up"


def power_ic_findings(b: dict, bom: dict, volts: dict, vsrc: dict,
                      netfns: dict, emit) -> bool:
    """An unextracted power IC's filled row against its rails (#805): each input
    pad against the top of the row's input range, which power_ic_inputs finds.
    Names the table doesn't hold, a rail with no volts, or a row with no number,
    judge nothing, so the part stays unrated. True when it emitted a finding: an
    in-limit part keeps its warning and unknown fact, as an in-rating regulator
    does (#708)."""
    rating = reg_vin_rating(bom)
    if not rating:
        return False
    vin, src = rating
    ref, pads = b["ref"], b["pads"]
    row = power_ic_row(str(bom.get("mpn") or ""))
    inputs = power_ic_inputs(b, row, netfns)
    found = False
    for net in sorted({pads[p] for p in inputs}):
        v = volts.get(net)
        if _no_trusted_volts(net, volts, vsrc) or v <= vin + EPS:
            continue
        on = sorted(p for p in inputs if pads[p] == net)
        who = "/".join(sorted({inputs[p] for p in on}))
        emit("rating_over_voltage", "error", ref, net,
             f"{ref} ({bom.get('mpn')}) {who} sits on {net} = {_fmt(v)} V, over "
             f"its {_fmt(vin)} V input rating [{src}; pad from {row['datasheet']}]",
             pin=f"{ref}.{on[0]}", net_v=v, limit_v=vin, rating=src)
        found = True
    return found


def power_ic_why_unrated(b: dict, bom: dict, volts: dict, vsrc: dict,
                        netfns: dict) -> str | None:
    """What became of a power IC's inputs, for its rating_unrated warning: why
    one wasn't compared (#807), else that all are within the row and only the
    other pins are unrated. None when the row has no number, which the usual
    words already say. Called only once power_ic_findings found nothing over."""
    rating = reg_vin_rating(bom)
    if not rating:
        return None
    limit = f"its {_fmt(rating[0])} V input rating"
    mpn, pads = bom.get("mpn"), b["pads"]
    row = power_ic_row(str(mpn or ""))
    inputs = power_ic_inputs(b, row, netfns)
    if not inputs:
        return (f"{mpn}: no pad here fits an input in {row['datasheet']}, by "
                f"name or footprint, so it is not judged against {limit}")
    parts, within = [], []
    for net in sorted({pads[p] for p in inputs}):
        who = "/".join(sorted({n for p, n in inputs.items() if pads[p] == net}))
        if _no_trusted_volts(net, volts, vsrc):
            what = "only a pull-up's guess at its volts" \
                if volts.get(net) is not None else "no volts"
            parts.append(f"{who} on {net} has {what}")
        else:
            within.append(f"{who} at {_fmt(volts[net])} V")
    if not parts:
        return (f"{mpn}: {', '.join(within)} within the {_fmt(rating[0])} V row; "
                "other pins unrated")
    return (f"{mpn}: {'; '.join(parts)}, so it is not judged against {limit}: "
            "give the rail a voltage in the constraints or the power tree")


def regulator_findings(b: dict, bom: dict | None, volts: dict, vsrc: dict,
                       netfns: dict, emit, fb_ctx: dict | None = None) -> bool:
    """An unextracted regulator's filled row against its rails (#708): the
    input and enable pads against the input rating, the output pad against
    what the row says it makes. Pads are told apart by name; with no pad names
    the highest-voltage net is taken as the input, unless the part steps up
    (its output is above its input on purpose), and the output isn't judged.
    A pull-up's volts are a guess at the pin, as in the supply rule of
    run_check, so they're not judged. A known family's MPN code also makes a
    part a regulator and says what it makes (#744), a switcher family makes it
    one too (#763, #774), and a fixed part's FB pad is its output. A regulator nothing
    gives an output for gets a warning, output pad named or not; that isn't a
    rating check, so it doesn't count as one. True when it emitted a finding."""
    if not bom or not bom.get("attributes"):
        return False
    mpn = str(bom.get("mpn") or "")
    code = reg_out_code(mpn) if REG_REF_RE.match(b["ref"]) else None
    if not reg_by_family(b["ref"], mpn) and not is_regulator(b, bom):
        return False
    ref, pads = b["ref"], b["pads"]
    known = {n: v for n, v in volts.items()
             if n in pads.values() and vsrc.get(n) != "pull-up"}
    names = {p: re.sub(r"_\d+$", "", fn.upper()) for p, fn in
             dict(netfns.get(ref) or {}, **b.get("fns") or {}).items()
             if p in pads}
    part = f"{ref} ({bom.get('mpn') or b['lcsc']})"
    found = False
    rating = reg_vin_rating(bom)
    if rating:
        vin, src = rating
        stressed = {p: n for p, n in names.items()
                    if REG_IN_PAD_RE.match(n) or REG_EN_PAD_RE.match(n)}
        how = ""
        if not names and known and not is_step_up(bom, vin):
            top = max(known, key=lambda n: known[n])
            stressed = {p: "" for p, n in pads.items() if n == top}
            how = "; no pad names, so its highest-voltage net is taken as " \
                  "its input"
        for net in sorted({pads[p] for p in stressed}):
            if known.get(net, 0.0) <= vin + EPS:
                continue
            on = sorted(p for p in stressed if pads[p] == net)
            who = "/".join(sorted({stressed[p] for p in on})) or "its input"
            emit("rating_over_voltage", "error", ref, net,
                 f"{part} {who} sits on {net} = {_fmt(known[net])} V, over "
                 f"its {_fmt(vin)} V input rating [{src}{how}]",
                 pin=f"{ref}.{on[0]}", net_v=known[net], limit_v=vin,
                 rating=src)
            found = True
    made = reg_out_range(bom)
    if made is None and code is not None:
        made = _fixed_range(code, f"MPN code {mpn}")
    nets = sorted({pads[p] for p, n in names.items()
                   if REG_OUT_PAD_RE.match(n) or
                   (n == "FB" and reg_fixed_part(mpn) is not None)})
    if made:
        sign = reg_sign(mpn, bom)
        for net in nets:
            # only a rail the board states: the MPN's own volts have no sign
            found |= _output_finding(
                ref, part, net, known.get(net), pads, made, emit,
                sign if vsrc.get(net) in RAIL_SOURCES else None)
    judged, hit = divider_findings(b, part, [b.get("value") or "", mpn],
                                   fb_pins_unextracted(b, names, mpn), fb_ctx,
                                   emit, fixed=made is not None)
    if judged:
        return found or hit
    if not made and not NOT_REG_RE.search(" ".join(str(bom.get(k) or "")
                                                   for k in REG_ROW_TEXT)):
        on = f"on {', '.join(nets)} " if nets else ""
        emit("rating_regulator_output_unjudged", "warning", ref,
             nets[0] if nets else None,
             f"{part} is a regulator, but its parts.json row and MPN don't "
             f"say what it makes, so its output {on}wasn't judged. Fill the "
             f"row's Output Voltage, or give it an extraction with "
             f"output_voltage")
    return found


def _output_finding(ref: str, part: str, net: str, v: float | None,
                    pads: dict, made: tuple, emit,
                    sign: tuple[int, str] | None = None) -> bool:
    """A rail outside what the part makes. Neither an MPN nor attr_volts
    carries a sign, so the magnitude is compared and -5 V is what a -5.0 part
    makes. sign (reg_sign) then adds what the part can't do whatever its
    magnitude (#744), given a pad on ground: with none its own ground is at
    another rail, and a positive part may sit below board ground on purpose."""
    lo, hi, src = made
    if v is None:
        return False
    on = sorted(p for p, n in pads.items() if n == net)
    if lo - EPS <= abs(v) <= hi + EPS:
        if not sign or sign[0] * v >= 0 or \
                not any(is_ground(n) for n in pads.values()):
            return False
        emit("rating_regulator_output", "error", ref, net,
             f"{part} output sits on {net} = {_fmt(v)} V, but it is a "
             f"{'positive' if sign[0] > 0 else 'negative'} regulator "
             f"[{sign[1]}; {src}]",
             pin=f"{ref}.{on[0]}", net_v=v, limit_v=hi, rating=src)
        return True
    emit("rating_regulator_output", "error", ref, net,
         f"{part} output sits on {net} = {_fmt(v)} V, which it can't make "
         f"[{src}]", pin=f"{ref}.{on[0]}", net_v=v, limit_v=hi, rating=src)
    return True


def reg_out_pins(b: dict, ext: dict) -> list[str]:
    """The board's pads an extraction calls a regulator's output."""
    return [p["pin"] for p in ext.get("pinout") or []
            if p["type"] == "power_out" and aliases(p["name"]) & REG_OUT_NAMES
            and p["pin"] in b["pads"]]


def fixed_fb_pins(b: dict, ext: dict, mpn: str) -> list[str]:
    """A fixed part's FB pads: it has no output pin, FB senses the rail (#774).
    An adjustable part's FB sits on a divider, so it isn't here. Only for
    judging: reg_out_pins, which the volts map reads, doesn't name a rail by
    this."""
    if reg_fixed_part(mpn) is None:
        return []
    return [p["pin"] for p in ext.get("pinout") or []
            if "FB" in aliases(p["name"]) and p["pin"] in b["pads"]]


def extraction_out_range(ext: dict) -> tuple[float, float, str] | None:
    """What an extraction says a part makes (#729): its output_voltage, else a
    fixed output its description names, else its MPN's code."""
    v = ext.get("output_voltage")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return _fixed_range(float(v), f"extraction output_voltage = {v} V")
    text = str(ext.get("description") or "")
    m = EXT_FIXED_RE.search(text)
    if m and "adjustable" not in text.lower():
        return _fixed_range(float(m.group(1) or m.group(2)),
                            f"extraction description: {m.group(0)}")
    mpn = str(ext.get("mpn") or "")
    v = reg_out_code(mpn)
    return _fixed_range(v, f"MPN code {mpn}") if v is not None else None


# --- adjustable regulators: the FB divider's Vout (#832) ----------------------

VREF_YAML = HERE.parent / "reference" / "regulator_vref.yaml"
# sim_board.FB_FN less VOS: on TI parts (TPS6208x) an output sense, not a tap
FB_PAD_RE = re.compile(r"^(V?FB\d?|ADJ|VSENSE)$")
TOL_ATTR_RE = re.compile(r"^Tolerance", re.I)


def load_vrefs(path: Path = VREF_YAML) -> list[dict]:
    """reference/regulator_vref.yaml's rows (sim_board reads the same)."""
    if not path.is_file():
        return []
    import yaml
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "regulators") or []


def find_vref(texts: list[str], table: list[dict]):
    """(vref, tol, source, verified) of the first row a value or MPN matches."""
    for txt in texts:
        for row in table:
            if txt and re.search(row["match"], txt, re.I):
                return (float(row["vref"]), float(row.get("tol", 0.02)),
                        row.get("source", "regulator_vref.yaml"),
                        bool(row.get("verified")))
    return None


BLOCK_REF_KEYS = ("name", "block", "ref", "refs")


def block_vouts(constraints: dict | None) -> dict[str, tuple[float, str]]:
    """ref -> (vout_v, block) from the constraints blocks that name a ref
    (in BLOCK_REF_KEYS) and give an operating_point vout_v."""
    out: dict[str, tuple[float, str]] = {}
    for blk in (constraints or {}).get("blocks") or []:
        v = (blk.get("operating_point") or {}).get("vout_v")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            continue
        label = str(blk.get("block") or blk.get("name") or "")
        # the ref may sit in the name ("U2 TPS54302 5 V") or a field of its
        # own (#847)
        text = " ".join(str(blk.get(k) or "") for k in BLOCK_REF_KEYS)
        for ref in re.findall(r"\b([A-Z]{1,3}\d+)\b", text):
            out.setdefault(ref, (float(v), label))
    return out


# --- the FB network (#847) ---------------------------------------------------
# Refdes classes on an FB network. A resistor is R plus an optional letter
# tag (RA1, Rfbt1), not a pot (RV), array (RN), relay (RLY) or thermistor
# (RT/RTH); a cap is C plus a tag, not a connector (CN/CON) or diode (CR).
FB_RES_RE = re.compile(r"^R[A-Za-z_]*\d")
FB_NOT_RES_RE = re.compile(r"^(RV|RN|RLY|RT|RTH)\d", re.I)
FB_CAP_RE = re.compile(r"^C[A-Za-z_]*\d")
FB_NOT_CAP_RE = re.compile(r"^(CN|CON|CR)\d", re.I)
FB_POT_RE = re.compile(r"^(RV|VR|POT|TRIM)\d", re.I)
FB_POT_FP_RE = re.compile(r"Potentiometer|Trimmer", re.I)
FB_TIE_FP_RE = re.compile(r"NetTie", re.I)
FB_DNP_VALUE_RE = re.compile(r"^(DNP|DNF|DNI|NC|N/C|NF|NP|NOPOP)\b", re.I)
FB_BEAD_RE = re.compile(r"^(L|FB|FL)\d")
FB_MAX_PARTS = 16
POT_TOL = 0.20      # a trim pot's end-to-end tolerance when none is stated
POT_STEPS = 20


def _is_dnp(r: dict) -> bool:
    return bool(r.get("dnp")) or bool(FB_DNP_VALUE_RE.match(
        str(r.get("value") or "").strip()))


def _fb_kind(r: dict) -> str:
    """res / pot / cap / tie / other: how a part reads on an FB network."""
    ref, fp = r["ref"], str(r.get("footprint") or "")
    pads = set(r["pads"])
    if re.match(r"^NT\d", ref) or FB_TIE_FP_RE.search(fp):
        return "tie"
    if (FB_POT_RE.match(ref) or FB_POT_FP_RE.search(fp)) and \
            pads <= {"1", "2", "3"} and len(r.get("nums") or pads) == 3:
        return "pot"
    if FB_RES_RE.match(ref) and not FB_NOT_RES_RE.match(ref):
        return "res"
    if FB_CAP_RE.match(ref) and not FB_NOT_CAP_RE.match(ref):
        return "cap"
    return "other"


def _r_tol(r: dict, ctx: dict) -> float:
    """A resistor's tolerance band as a fraction (1 % floor)."""
    try:
        import tolerance
        p = tolerance.part_tolerance(
            "R", r["value"], _attr(ctx["bom_by_lcsc"].get(r["lcsc"]),
                                   TOL_ATTR_RE), nom=1.0)
        if p is not None:
            return max(0.01, 1 - p.lo, p.hi - 1)
    except Exception:  # noqa: BLE001 - a band we can't read: 1 %
        pass
    return 0.01


def _solve_fb(edges: list[tuple[str, str, float]], fixed: dict[str, float],
              fb: str) -> float | None:
    """V(fb) of a resistor network (edges: net, net, ohms) with the fixed
    nets held at their volts and no current out of the rest (nodal analysis,
    Gaussian elimination). None when fb floats."""
    nodes = sorted({n for a, b, _ in edges for n in (a, b)} - set(fixed))
    if fb in fixed:
        return fixed[fb]
    if fb not in nodes:
        return None
    ix = {n: i for i, n in enumerate(nodes)}
    k = len(nodes)
    g = [[0.0] * (k + 1) for _ in range(k)]
    for a, b, ohm in edges:
        y = 1.0 / max(ohm, 1e-6)
        for p, q in ((a, b), (b, a)):
            if p in ix:
                g[ix[p]][ix[p]] += y
                if q in ix:
                    g[ix[p]][ix[q]] -= y
                else:
                    g[ix[p]][k] += y * fixed[q]
    for c in range(k):
        piv = max(range(c, k), key=lambda i: abs(g[i][c]))
        if abs(g[piv][c]) < 1e-15:
            return None
        g[c], g[piv] = g[piv], g[c]
        for i in range(k):
            if i != c and g[i][c]:
                f = g[i][c] / g[c][c]
                g[i] = [x - f * y for x, y in zip(g[i], g[c])]
    return g[ix[fb]][k] / g[ix[fb]][ix[fb]]


def _fb_network(b: dict, fb: str, ctx: dict) -> dict:
    """Walk the resistor network hanging off a regulator's FB net. Caps are
    open at DC, net ties and 0 ohm parts merge nets, DNP parts are open; a net
    that carries only resistors, caps and ties is internal, any other net
    (ground, a stated rail, a net with another part on it) is a terminal.
    {'reason': why not judged} or {fb, out, how, ratio_lo, ratio_nom,
    ratio_hi, notes}: the ratio Vout/Vfb with every resistor at the edge of
    its band and any trim pot over its whole travel."""
    board = ctx["board"]
    if ctx["vsrc"].get(fb) in RAIL_SOURCES:
        return {"fb": fb, "out": fb, "how": f"FB on {fb}", "plain": False,
                "ratio_lo": 1.0, "ratio_nom": 1.0, "ratio_hi": 1.0,
                "notes": [], "top": None, "pots": False}
    parent: dict[str, str] = {}

    def find(n: str) -> str:
        while parent.get(n, n) != n:
            n = parent[n]
        return n

    notes: list[str] = []
    elems = []          # (part, kind)
    for r in board:
        if r["ref"] == b["ref"] or not r["pads"]:
            continue
        kind = _fb_kind(r)
        if kind == "other":
            continue
        if _is_dnp(r):
            elems.append((r, "dnp"))
            continue
        nets = set(r["pads"].values())
        if kind == "tie" or (kind == "res" and len(nets) == 2 and
                             engcalc.parse_value(r["value"]) == 0 and
                             re.match(r"^\s*0", str(r["value"]))):
            if len(nets) == 2:
                a, c = (find(x) for x in sorted(nets))
                if a != c:
                    parent[max(a, c)] = min(a, c)
            continue
        elems.append((r, kind))
    fbn = find(fb)
    by_net: dict[str, list] = {}
    for r, kind in elems:
        for n in {find(x) for x in r["pads"].values()}:
            by_net.setdefault(n, []).append((r, kind))
    others: dict[str, list[str]] = {}
    for r in board:
        if (_fb_kind(r) != "other" or _is_dnp(r)) and r["ref"] != b["ref"]:
            continue
        for p, n in r["pads"].items():
            if r["ref"] == b["ref"] and find(n) == fbn:
                continue        # the regulator's own FB pad
            others.setdefault(find(n), [])
            if r["ref"] not in others[find(n)]:
                others[find(n)].append(r["ref"])
    if others.get(fbn):
        return {"reason": f"{fb} also carries {', '.join(others[fbn][:3])}, "
                f"which can move the output, so the divider alone doesn't "
                f"set it"}
    stated = {find(n) for n, s in ctx["vsrc"].items() if s in RAIL_SOURCES}

    def terminal(n: str) -> bool:
        # a net reached by one resistor and nothing else is where the
        # divider senses the output (an unnamed rail with no other part)
        return n != fbn and (
            is_ground(n) or bool(others.get(n)) or n in stated
            or len([e for e in by_net.get(n, []) if e[1] != "dnp"]) == 1)

    seen, todo, used = {fbn}, [fbn], {}
    while todo:
        n = todo.pop()
        if n != fbn and terminal(n):
            continue
        for r, kind in by_net.get(n, []):
            if kind in ("cap",) or r["ref"] in used:
                continue
            if kind == "dnp":
                notes.append(f"{r['ref']} is DNP (left open)")
                used[r["ref"]] = (r, kind)
                continue
            used[r["ref"]] = (r, kind)
            for m in {find(x) for x in r["pads"].values()}:
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
        if len(used) > FB_MAX_PARTS:
            return {"reason": f"the network on {fb} has more than "
                    f"{FB_MAX_PARTS} parts, more than this check solves"}
    res = [(r, k) for r, k in used.values() if k in ("res", "pot")]
    if not res:
        return {}
    edges0, tols, pots = [], [], []
    for r, kind in res:
        v = engcalc.parse_value(r["value"])
        if not v:
            return {"reason": f"{r['ref']}'s value {r['value']!r} doesn't "
                    f"parse, so the divider's ratio is unknown"}
        if kind == "res":
            nets = sorted({find(x) for x in r["pads"].values()})
            if len(nets) != 2:
                continue
            edges0.append((nets[0], nets[1], v))
            tols.append(_r_tol(r, ctx))
        else:
            pots.append((r, v))
    terms = sorted(n for n in seen if n != fbn and terminal(n))
    gnd = [n for n in terms if is_ground(n)]
    tops = [n for n in terms if not is_ground(n)]
    if not tops:
        return {"reason": f"no resistor on {fb} reaches an output or rail "
                f"(only {', '.join(gnd) or 'nothing'}), so there is no "
                f"divider to read"}
    if len(tops) > 1:
        return {"reason": f"the network on {fb} is fed from "
                f"{', '.join(tops[:4])}, so its output depends on more than "
                f"one source"}
    out = tops[0]
    rtol = max(tols or [0.01])

    def ratio(scale: list[float], wipers: list[float],
              pot_scale: float = 1.0) -> float | None:
        edges = [(a, c, ohm * s) for (a, c, ohm), s in zip(edges0, scale)]
        for (r, tot), w in zip(pots, wipers):
            p = {k: find(n) for k, n in r["pads"].items()}
            tot *= pot_scale
            if "2" not in p:            # wiper open: the whole track
                if "1" in p and "3" in p:
                    edges.append((p["1"], p["3"], tot))
                continue
            for end, part in (("1", w), ("3", 1 - w)):
                if end in p:
                    edges.append((p[end], p["2"], max(tot * part, 1e-3)))
        fixed = {n: 0.0 for n in gnd}
        fixed[out] = 1.0
        v = _solve_fb(edges, fixed, fbn)
        return 1.0 / v if v and v > 1e-9 else None

    nom_w = [0.5] * len(pots)
    nom = ratio([1.0] * len(edges0), nom_w)
    if nom is None:
        return {"reason": f"{fb} sits at 0 V through the network, so no "
                f"output sets it"}
    # each resistor at the band edge that pushes the ratio up (or down)
    signs = []
    for i in range(len(edges0)):
        s = [1.0] * len(edges0)
        s[i] = 1.0 + rtol
        up = ratio(s, nom_w)
        signs.append(1.0 if up is not None and up >= nom else -1.0)
    grid = [[]]
    for _ in pots:
        grid = [g + [i / POT_STEPS] for g in grid for i in range(POT_STEPS + 1)]
    los, his = [], []
    for w in grid:
        for d, ps in ((1.0, 1 + POT_TOL), (-1.0, 1 - POT_TOL),
                      (1.0, 1 - POT_TOL), (-1.0, 1 + POT_TOL)):
            if not pots and ps != 1 + POT_TOL:
                continue
            x = ratio([1 + d * sg * rtol for sg in signs], w,
                      ps if pots else 1.0)
            if x is not None:
                (his if d > 0 else los).append(x)
    if not los or not his:
        return {"reason": f"{fb}'s network can't be solved at its corners"}
    parts = sorted(r["ref"] for r, _ in res)
    plain = not pots and len(res) == 2 and len(gnd) == 1
    if plain:
        top = next(r for r, _ in res if out in {find(x) for x in
                                                 r["pads"].values()})
        bot = next(r for r, _ in res if r is not top)
        how = f"{top['ref']} {top['value']} / {bot['ref']} {bot['value']} " \
              f"on {fb}"
    else:
        names = ", ".join(f"{r['ref']} {r['value']}" for r, _ in
                          sorted(res, key=lambda x: x[0]["ref"]))
        trims = ", ".join(r["ref"] for r, _ in pots)
        how = (f"the network {names} on {fb}"
               + (f" (trim {trims} over its whole travel)" if pots else ""))
    return {"fb": fb, "out": out, "how": how, "plain": plain,
            "ratio_lo": min(los), "ratio_nom": nom, "ratio_hi": max(his),
            "notes": notes, "top": parts, "pots": bool(pots)}


def _rail_beyond(net: str, b: dict, ctx: dict) -> str | None:
    """A stated rail one 2-pad bead or inductor beyond net, when the
    regulator itself has no pad on net (else net may be its switch node)."""
    if net in b["pads"].values():
        return None
    for r in ctx["board"]:
        nets = set(r["pads"].values())
        if FB_BEAD_RE.match(r["ref"]) and len(r["pads"]) == 2 and \
                len(nets) == 2 and net in nets and not _is_dnp(r):
            (o,) = nets - {net}
            if ctx["vsrc"].get(o) in RAIL_SOURCES:
                return o
    return None


def fb_divider(b: dict, fb_pins: list[str], ctx: dict) -> dict | None:
    """The divider on a regulator's FB pad (_fb_network): {fb, out, rail,
    how, ratio_lo, ratio_nom, ratio_hi, notes}, or {reason} when the network
    can't be read, or None when the FB pad has no resistor on it."""
    first = None
    for fb in sorted({b["pads"][p] for p in fb_pins if p in b["pads"]}):
        if is_ground(fb):
            continue
        net = _fb_network(b, fb, ctx)
        if net.get("out"):
            out = net["out"]
            net["rail"] = out if ctx["vsrc"].get(out) in RAIL_SOURCES else \
                _rail_beyond(out, b, ctx)
            return net
        if net.get("reason") and first is None:
            first = {**net, "fb": fb}
    return first


def worst_case_top(fbv: dict) -> float:
    """The highest worst case a rail stated in constraints `voltages` may
    give over a divider that makes fbv (engcalc.fb_vout): REG_OUT_TOL over
    its nominal, as a fixed part's rail may sit, and never under its window."""
    return max(fbv["max"], fbv["nom"] * (1 + REG_OUT_TOL))


def worst_case_fits(fbv: dict, v: float) -> bool:
    """constraints `voltages` give a rail's worst case, not its set point
    (reference/constraints_schema.md), so the figure fits from the divider's
    lowest output up to worst_case_top: +5V's 5.25 V over a 4.99 V divider is
    that rail, over a 3.32 V one it is not."""
    return fbv["min"] - EPS <= abs(v) <= worst_case_top(fbv) + EPS


def divider_findings(b: dict, part: str, texts: list[str],
                     fb_pins: list[str], ctx: dict | None, emit,
                     fixed: bool = False) -> tuple[bool, bool]:
    """An adjustable regulator's output from its FB divider and the datasheet
    Vref (reference/regulator_vref.yaml), against what the board says it makes:
    the rail its top resistor sits on, when the board states that rail, and
    the vout_v of a constraints block that names the part (#832). Only a
    stated value outside the worst-case window (engcalc.fb_vout over Vref and
    resistor tolerance) is an error, except that a rail's constraints voltage
    is its worst case and may sit above the window (worst_case_fits). An FB
    pad straight on a rail is judged only with a known Vref and when nothing
    calls the part fixed (fixed: its row, extraction or MPN names an output,
    so FB may be its output sense).
    (judged, emitted): judged when a divider was found, so the output needs
    no other 'unjudged' warning; emitted when it raised a finding."""
    if not ctx or not fb_pins:
        return False, False
    div = fb_divider(b, fb_pins, ctx)
    if div is None:
        return False, False
    ref = b["ref"]
    if div.get("reason"):
        if fixed:       # FB may be a fixed part's output sense
            return False, False
        emit("rating_regulator_output_unjudged", "warning", ref, div["fb"],
             f"{part}'s output wasn't judged from its FB divider: "
             f"{div['reason']}")
        ctx.setdefault("not_judged", []).append(
            f"{ref} output: {div['reason']}")
        return True, False
    vr = find_vref(texts, ctx["vrefs"])
    if not div["top"] and (fixed or vr is None):
        return False, False
    how = div["how"] + "".join(f"; {n}" for n in div["notes"])
    rail = div.get("rail")
    if vr is None:
        emit("rating_regulator_output_unjudged", "warning", ref, div["out"],
             f"{part} sets its output with {how}, but its Vref isn't in "
             f"reference/regulator_vref.yaml, so the output wasn't judged. "
             f"Add a row with the datasheet's VFB")
        return True, False
    vref, vtol, src, verified = vr
    # engcalc.fb_vout's corners, over the whole network (_fb_network)
    fbv = {"nom": vref * div["ratio_nom"],
           "min": vref * (1 - vtol) * div["ratio_lo"],
           "max": vref * (1 + vtol) * div["ratio_hi"]}
    lo, hi = fbv["min"], fbv["max"]
    rating = f"Vref {_fmt(vref)} V ±{_fmt(vtol * 100)}% ({src}" + \
        ("" if verified else "; unverified") + ")"
    stated = []
    via = (f" (through the bead or inductor from {div['out']})"
           if rail and rail != div["out"] else "")
    if rail:
        v = ctx["volts"][rail]
        if ctx["vsrc"][rail] == "constraints":
            stated.append((rail, v, True,
                           f"constraints give {rail}{via} a worst case of "
                           f"{_fmt(v)} V (over this divider it may be "
                           f"{_fmt(lo)}-{_fmt(worst_case_top(fbv))} V)"))
        else:
            stated.append((rail, v, False, f"{rail}{via} = {_fmt(v)} V"))
    if ref in ctx["blocks"]:
        v, blk = ctx["blocks"][ref]
        stated.append((rail or div["out"], v, False,
                       f"constraints {blk} vout_v = {_fmt(v)} V"))
    on = sorted(p for p, n in b["pads"].items() if n == div["fb"])
    emitted = False
    for net, v, worst, what in stated:
        if worst_case_fits(fbv, v) if worst else lo - EPS <= abs(v) <= hi + EPS:
            continue
        emit("rating_regulator_output", "error", ref, net,
             (f"{part} can be trimmed to {_fmt(lo)}-{_fmt(hi)} V "
              if div.get("pots") else
              f"{part} makes {_fmt(fbv['nom'])} V ({_fmt(lo)}-{_fmt(hi)} V) ")
             + f"from {how}, but {what} [{rating}]",
             pin=f"{ref}.{on[0]}" if on else None, net_v=v,
             limit_v=fbv["nom"], rating=rating)
        emitted = True
    return True, emitted


def fb_pins_unextracted(b: dict, names: dict, mpn: str) -> list[str]:
    """An unextracted adjustable part's FB pads, by pad name."""
    if reg_fixed_part(mpn) is not None:
        return []
    return [p for p, n in names.items()
            if any(FB_PAD_RE.match(a) for a in aliases(n))]


def fb_pins_extracted(b: dict, ext: dict, mpn: str) -> list[str]:
    """An extracted adjustable part's FB pads, by pinout name."""
    if reg_fixed_part(mpn) is not None:
        return []
    return [p["pin"] for p in ext.get("pinout") or []
            if p["pin"] in b["pads"]
            and any(FB_PAD_RE.match(a) for a in aliases(p["name"]))]


def extracted_output_findings(b: dict, ext: dict, bom: dict | None,
                              volts: dict, vsrc: dict, emit,
                              fb_ctx: dict | None = None) -> None:
    """An extracted regulator's output pin against its rail (#721), read as
    regulator_findings reads a filled row: the row's output, else a fixed
    output in the extraction's or the row's MPN, else what the extraction says
    (extraction_out_range). A fixed part's FB pad is its output (#774). Only a
    rail the board itself states: a pull-up's volts are a guess, and a volt set
    from this part's or another's MPN isn't a rail to judge a part against. A
    regulator nothing gives an output for gets a warning instead of silence
    (#729)."""
    bom = bom or {}
    mpn = str(ext.get("mpn") or "")
    known_mpn = mpn or str(bom.get("mpn") or "")
    row = dict(bom, mpn=mpn) if reg_out_volts(mpn) is not None else bom
    desc = str(ext.get("description") or "")
    from_ext = extraction_out_range(ext)
    fixed = reg_fixed_part(known_mpn)
    if from_ext is None and fixed is not None:
        from_ext = _fixed_range(fixed, f"MPN code {known_mpn}")
    if not (is_regulator(b, row) or (REG_REF_RE.match(b["ref"]) and (
            from_ext or EXT_REG_RE.search(desc)))):
        return
    made = reg_out_range(row) or from_ext
    part = f"{b['ref']} ({known_mpn or b['lcsc']})"
    nets = sorted({b["pads"][p] for p in reg_out_pins(b, ext) +
                   fixed_fb_pins(b, ext, known_mpn)})
    if made:
        sign = reg_sign(known_mpn, bom, desc)
        for net in nets:
            if vsrc.get(net) in RAIL_SOURCES:
                _output_finding(b["ref"], part, net, volts[net], b["pads"],
                                made, emit, sign)
    if divider_findings(b, part, [b.get("value") or "", known_mpn],
                        fb_pins_extracted(b, ext, known_mpn), fb_ctx,
                        emit, fixed=made is not None)[0]:
        return
    if not made and nets and not NOT_REG_RE.search(" ".join(
            [desc] + [str(bom.get(k) or "") for k in REG_ROW_TEXT])):
        emit("rating_regulator_output_unjudged", "warning", b["ref"], nets[0],
             f"{part} is a regulator, but its parts.json row, extraction and "
             f"MPN don't say what it makes, so its output on "
             f"{', '.join(nets)} wasn't judged. Set output_voltage in its "
             f"extraction, or fill the row's Output Voltage")


def no_attribute_findings(board: list[dict], bom_by_lcsc: dict, exts: dict,
                          trusted: set, emit, out: list[dict]) -> None:
    """Rated parts whose parts.json row has an empty `attributes` and which
    nothing else rates: one blocking finding per BOM code, since that is what
    gets filled (#675). A diode, cap or regulator with an extraction is rated
    by it; a fuse is not, fuse_findings reads only attributes. A known family's
    MPN code (#751) or switcher family (#763, #774) makes a U/VR/IC part a
    regulator here, as in regulator_findings; a power-IC family (#797) makes it
    rated here too. A part with no row at all is not
    here: there is nothing to fill yet (the scaffold's empty parts/). This never
    reads the cache or the network, so the same board gives the same answer
    anywhere."""
    by_code: dict[str, tuple[list[str], set[str]]] = {}
    for b in board:
        bom = bom_by_lcsc.get(b["lcsc"])
        ref = b["ref"]
        if bom is None or bom.get("attributes") or ref in trusted or \
                len(set(b["pads"].values())) < 2:
            continue
        what = None
        if is_fuse(b, bom):
            what = "fuse"
        elif b["lcsc"] in exts:
            continue
        elif re.match(r"^(?:D|LED)\d", ref):
            what = "diode"
        elif re.match(r"^C\d", ref):
            what = "capacitor"
        elif is_regulator(b, bom) or \
                reg_by_family(ref, str(bom.get("mpn") or "")):
            what = "regulator"
        elif power_ic_by_family(ref, str(bom.get("mpn") or "")):
            what = "power IC"
        if what:
            refs, whats = by_code.setdefault(b["lcsc"], ([], set()))
            refs.append(ref)
            whats.add(what)
    for lcsc, (refs, whats) in sorted(by_code.items()):
        refs = sorted(refs)
        mpn = bom_by_lcsc[lcsc].get("mpn") or "no MPN"
        emit("rating_no_attributes", "error", refs[0], None,
             f"{', '.join(refs)}: {lcsc} ({mpn}) is a rated part "
             f"({'/'.join(sorted(whats))}) whose parts.json row has no "
             "attributes and nothing else rates it, so its rating was never "
             "judged. Fill the row with scripts/parts_enrich.py --workspace "
             "<ws> (or keep parts_search's row whole), then run verify again",
             lcsc=lcsc, mpn=mpn)
        out[-1]["refs"] = refs


# --- ambient: fuses, PPTCs and every part's operating range -------------------

DEFAULT_AMBIENT_C = 25.0
HOLD_ATTR_RE = re.compile(r"hold(?:ing)?\s*current|current.*hold|\(ih\)", re.I)
FUSE_I_ATTR_RE = re.compile(r"current.*rat|rated.*current|^current$", re.I)
FUSE_V_ATTR_RE = re.compile(r"volt.*(?:max|rat)|rated.*volt|^voltage", re.I)
PPTC_TEXT_RE = re.compile(r"PPTC|resettable|polyfuse|polyswitch|multifuse|"
                          r"\bPTC\b", re.I)
TEMP_ATTR_RE = re.compile(r"operating temp", re.I)


def ambient(cons: dict | None, cons_dir: Path | None) -> dict:
    """The air the board's parts sit in: {c, min_c, src}. constraints
    `ambient_c`, else the enclosure's inside air as check_thermal works it
    out (room_c plus the thermal entries' heat through the walls), else
    25 C, said so in `src`. `ambient_min_c` is the coldest it starts in
    (None: the cold end is not checked)."""
    cons = cons or {}
    lo = cons.get("ambient_min_c")
    lo = float(lo) if isinstance(lo, (int, float)) else None
    if isinstance(cons.get("ambient_c"), (int, float)):
        return {"c": float(cons["ambient_c"]), "min_c": lo,
                "src": "constraints ambient_c"}
    if isinstance(cons.get("enclosure"), dict):
        try:
            import check_thermal
            heat = sum(float(e.get("power_w", 0) or 0)
                       for e in cons.get("thermal") or [])
            air, _, _ = check_thermal.enclosure_air(
                cons["enclosure"], heat, cons_dir or Path("."))
            return {"c": round(float(air), 1), "min_c": lo,
                    "src": "enclosure air (check_thermal)"}
        except Exception as exc:  # noqa: BLE001
            room = cons["enclosure"].get("room_c", 30)
            if isinstance(room, (int, float)):
                return {"c": float(room), "min_c": lo,
                        "src": f"enclosure room_c (box air not worked out: "
                               f"{str(exc)[:80]})"}
    return {"c": DEFAULT_AMBIENT_C, "min_c": lo,
            "src": "default 25 C: constraints carry no ambient_c"}


def _attr(bom: dict | None, rx: re.Pattern) -> str | None:
    for a in (bom or {}).get("attributes") or []:
        if rx.search(str(a.get("name") or "")):
            return str(a.get("value") or "")
    return None


def is_fuse(b: dict, bom: dict | None) -> bool:
    if len(set(b["pads"].values())) != 2 and not re.match(r"^F\d", b["ref"]):
        return False
    return bool(re.match(r"^(?:F|FU)\d", b["ref"])) or \
        _attr(bom, HOLD_ATTR_RE) is not None


def is_pptc(bom: dict | None) -> bool:
    if _attr(bom, HOLD_ATTR_RE) is not None:
        return True
    text = " ".join(str((bom or {}).get(k) or "") for k in
                    ("category", "description", "mpn", "name", "value"))
    return bool(PPTC_TEXT_RE.search(text))


def fuse_findings(board: list[dict], bom_by_lcsc: dict, volts: dict,
                  vsrc: dict, currents: dict, amb: dict, emit,
                  checked: dict, unrated_passives: list[str]) -> list[dict]:
    """Fuses and PPTCs (F*, or a parts.json hold-current attribute) at the
    board's ambient (R-RAT-3, R-RAT-4). The current is the larger
    constraints power[].current_a of its two nets; its voltage is the
    larger rail it sits on, against ground: a fuse that opens must hold off
    the circuit's voltage, not the millivolts across it while it conducts.
      fuse: I_op over 0.75 x k_T x I_rated warns (it ages open), over
            k_T x I_rated is an error;
      PPTC: Ihold(T) = Ihold x the typical derating table, under I_op is an
            error (it trips in normal running), under 1.1 x I_op warns;
      either: a voltage rating (Vmax) under the rail is an error."""
    t = amb["c"]
    out = []
    for b in sorted(board, key=lambda x: x["ref"]):
        bom = bom_by_lcsc.get(b["lcsc"])
        if not b["pads"] or not is_fuse(b, bom):
            continue
        ref, nets = b["ref"], sorted(set(b["pads"].values()))
        pptc = is_pptc(bom)
        kind = "PPTC" if pptc else "fuse"
        irated = engcalc.parse_amps(_attr(bom, HOLD_ATTR_RE) if pptc else
                                    _attr(bom, FUSE_I_ATTR_RE))
        vrated = engcalc.parse_volts(_attr(bom, FUSE_V_ATTR_RE))
        iop = max((currents[n] for n in nets if n in currents), default=None)
        vs = [abs(volts[n]) for n in nets if n in volts]
        vop = max(vs) if vs else None
        fact = {"ref": ref, "kind": kind, "rated_a": irated,
                "rated_v": vrated, "op_a": iop, "op_v": vop, "ambient_c": t}
        out.append(fact)
        if irated is None and vrated is None:
            unrated_passives.append(ref)
            continue
        net = nets[0]
        did = False
        if vrated is not None and vop is not None:
            did = True
            if vop > vrated + EPS:
                emit("rating_over_voltage", "error", ref, net,
                     f"{ref} ({kind}) is rated {_fmt(vrated)} V but sits on "
                     f"{_fmt(vop)} V ({'/'.join(vsrc[n] for n in nets if n in vsrc)}): "
                     "once it opens it must hold off the rail [parts.json]",
                     limit_v=vrated, net_v=vop, rating="parts.json")
        if irated is not None and iop is not None:
            did = True
            if pptc:
                k = engcalc.pptc_hold_factor(t)
                hold = irated * k
                fact.update(hold_at_ambient_a=round(hold, 4), k=round(k, 3))
                why = (f"{ref} (PPTC) holds {_fmt(irated)} A at 23 C, "
                       f"{_fmt(hold)} A at {_fmt(t)} C ({amb['src']}; x"
                       f"{k:.2f}, typical Bourns/Littelfuse derating - the "
                       f"part's own table wins), against {_fmt(iop)} A on "
                       f"{'/'.join(n for n in nets if n in currents)} "
                       "(constraints power[])")
                if iop > hold + EPS:
                    emit("rating_over_current", "error", ref, net,
                         why + ": it trips in normal running", limit_a=hold,
                         current_a=iop, ambient_c=t)
                elif iop * engcalc.PPTC_MARGIN > hold + EPS:
                    emit("rating_over_current", "warning", ref, net,
                         why + ": under the 10 % hold margin", limit_a=hold,
                         current_a=iop, ambient_c=t)
            else:
                k = engcalc.fuse_rerate(t)
                cap = irated * k
                use = cap * engcalc.FUSE_LOAD_FRACTION
                fact.update(rerated_a=round(cap, 4), k=round(k, 3))
                why = (f"{ref} (fuse) is rated {_fmt(irated)} A, "
                       f"{_fmt(cap)} A at {_fmt(t)} C ({amb['src']}; k_T "
                       f"{k:.2f}), against {_fmt(iop)} A on "
                       f"{'/'.join(n for n in nets if n in currents)} "
                       "(constraints power[])")
                if iop > cap + EPS:
                    emit("rating_over_current", "error", ref, net,
                         why + ": over its rerated current", limit_a=cap,
                         current_a=iop, ambient_c=t)
                elif iop > use + EPS:
                    emit("rating_over_current", "warning", ref, net,
                         why + f": over 75 % of it ({_fmt(use)} A), where a "
                         "fuse ages and opens on its own", limit_a=use,
                         current_a=iop, ambient_c=t)
        if did:
            checked[ref] = checked.get(ref, 0) + 1
    return out


def temperature_findings(board: list[dict], bom_by_lcsc: dict, exts: dict,
                         amb: dict, emit) -> dict:
    """Every part's operating temperature range (parts.json "Operating
    Temperature", else the extraction's `operating_temp_c` [lo, hi])
    against the board's ambient (R-REL-2): an ambient above the part's
    maximum, or an `ambient_min_c` below its minimum, is an error; a range
    LCSC gives for the junction only (@(Tj)) is held against the air too, a
    bound the air must not pass. The part's own rise comes on top
    (check_thermal), so passing here is necessary, not sufficient."""
    t, lo_amb = amb["c"], amb["min_c"]
    checked, ranges = 0, {}
    for b in sorted(board, key=lambda x: x["ref"]):
        if not b["pads"]:
            continue
        rng = engcalc.parse_temp_range(_attr(bom_by_lcsc.get(b["lcsc"]),
                                             TEMP_ATTR_RE))
        src = "parts.json Operating Temperature"
        ot = (exts.get(b["lcsc"]) or {}).get("operating_temp_c")
        if rng is None and isinstance(ot, list) and len(ot) == 2 and \
                all(isinstance(x, (int, float)) for x in ot):
            rng, src = (float(ot[0]), float(ot[1]), False), "extraction"
        if rng is None:
            continue
        lo, hi, tj = rng
        checked += 1
        ranges[f"{_fmt(lo)}..{_fmt(hi)}"] = ranges.get(
            f"{_fmt(lo)}..{_fmt(hi)}", 0) + 1
        what = "junction " if tj else ""
        if t > hi + EPS:
            emit("rating_over_temperature", "error", b["ref"], None,
                 f"{b['ref']} operates to {_fmt(hi)} C ({what}max, {src}) "
                 f"but the board's air is {_fmt(t)} C ({amb['src']})",
                 limit_c=hi, ambient_c=t, rating=src)
        elif lo_amb is not None and lo_amb < lo - EPS:
            emit("rating_over_temperature", "error", b["ref"], None,
                 f"{b['ref']} operates down to {_fmt(lo)} C ({src}) but the "
                 f"board starts at {_fmt(lo_amb)} C (constraints "
                 "ambient_min_c)", limit_c=lo, ambient_c=lo_amb, rating=src)
    return {"parts_checked": checked, "ranges": dict(sorted(ranges.items()))}


VR_ATTR_RE = re.compile(r"reverse.*volt|\(vr\)|\bvrrm\b", re.I)
INPUT_CONN_RE = re.compile(r"^(?:J|P|CN|CON|X|BT)\d")
SERIES_RE = re.compile(r"^(?:R|L|FB)\d")


def net_pinfunctions(pcb: Path) -> dict[str, dict[str, str]]:
    """{ref: {pad: pinfunction}} from the schematic netlist beside the board
    (<board>.net, else the only *.net there): a board written without
    pinfunctions still says which diode pad is the cathode."""
    cands = [pcb.with_suffix(".net")]
    if not cands[0].is_file():
        cands = sorted(pcb.parent.glob("*.net"))
        if len(cands) != 1:
            return {}
    try:
        root = sexpdata.loads(cands[0].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, str]] = {}
    for nets in _kids(root, "nets"):
        for n in _kids(nets, "net"):
            for node in _kids(n, "node"):
                f = {_t(k[0]): k[1] for k in node[1:]
                     if isinstance(k, list) and len(k) > 1}
                if f.get("ref") and f.get("pin") and f.get("pinfunction"):
                    out.setdefault(str(f["ref"]), {})[str(f["pin"])] = \
                        str(f["pinfunction"])
    return out


def diode_reverse(b: dict, ext: dict | None, bom: dict | None,
                  netfns: dict) -> dict | None:
    """A 2-terminal diode's reverse-voltage rating and its pads:
    {k, a, vr, src, how, tier, bi}. The rating is the extraction's reverse
    row (rated K against A), else a parts.json reverse-voltage attribute,
    else the family table its part number belongs to (diode_vr: SS14 = 40 V,
    a TVS's standoff; tier part_code, bi for a part that blocks both ways);
    the cathode is the extraction's K pin, else the pad named K/cathode,
    else pad 1 (the KiCad diode and LED convention, said in `how`). None for
    anything else."""
    if not re.match(r"^(?:D|LED)\d", b["ref"]) or len(b["pads"]) != 2:
        return None
    vr = src = None
    k = a = None
    how = "pinout"
    tier, bi = "datasheet", False
    if ext:
        idx = pin_index(ext.get("pinout") or [])
        ks, as_ = idx.get("K") or [], idx.get("A") or []
        if ks and as_:
            k, a = ks[0]["pin"], as_[0]["pin"]
            for r in part_ratings(ext).get(k, {}).get(("voltage", "abs"), []):
                if r["ref"] == as_[0]["name"] and isinstance(r["hi"], float):
                    if vr is None or r["hi"] < vr:
                        vr, src = r["hi"], r["src"]
    if vr is None:
        for att in (bom or {}).get("attributes") or []:
            if VR_ATTR_RE.search(str(att.get("name") or "")):
                m = re.search(r"(\d+(?:\.\d+)?)\s*(k?)V",
                              str(att.get("value") or ""))
                if m:
                    vr = float(m.group(1)) * (1000 if m.group(2) else 1)
                    src = f"parts.json {att.get('name')} = {att.get('value')}"
                    tier = "distributor"
    if vr is None and (code := diode_code_rating(b, bom)):
        vr, src, bi = code["v"], code["src"], code["bi"]
        tier = "part_code"
    if vr is None:
        return None
    if k is None:
        k, a, how = _cathode_by_pads(b, netfns)
    if k not in b["pads"] or a not in b["pads"]:
        return None
    return {"k": k, "a": a, "vr": vr, "src": src, "how": how, "tier": tier,
            "bi": bi}


def diode_code_rating(b: dict, bom: dict | None) -> dict | None:
    """{v, bi, src} for a D*/LED* part whose part number (the board's MPN,
    else parts.json's, else its Value) belongs to a family in
    reference/diode_vr.yaml; None for a family or code with no row."""
    if not re.match(r"^(?:D|LED)\d", b["ref"]):
        return None
    for pn in (b.get("mpn"), (bom or {}).get("mpn"), b.get("value")):
        if code := diode_vr.mpn_reverse(pn or ""):
            what = "standoff VRWM" if code["rating"] == "VRWM" else \
                f"reverse rating {code['rating']}"
            return {"v": code["v"], "bi": code["bi"],
                    "src": f"part number {pn}: {code['family']} {what} "
                           f"{_fmt(code['v'])} V"
                           + (", bidirectional" if code["bi"] else "")
                           + f" ({code['cite']})"}
    return None


def _diode_reverse_v(dio: dict, vk: float, va: float) -> float:
    """The volts a diode is asked to block: cathode over anode, or either
    way round for a bidirectional part."""
    return abs(vk - va) if dio.get("bi") else vk - va


def _cathode_by_pads(b: dict, netfns: dict) -> tuple[str, str, str]:
    """(cathode pad, anode pad, how) from the pads' K/A names, else pad 1 as
    the cathode (the KiCad diode and LED convention)."""
    k = a = None
    fns = dict(netfns.get(b["ref"]) or {}, **b.get("fns") or {})
    for pad, fn in fns.items():
        name = re.sub(r"_\d+$", "", fn.upper())
        if name in ("K", "CATHODE"):
            k = pad
        elif name in ("A", "ANODE"):
            a = pad
    if k is None or a is None:
        return "1", "2", "pad 1 taken as the cathode (KiCad's diode convention)"
    return k, a, "pad names"


def diode_pads(b: dict, ext: dict | None, netfns: dict) -> dict | None:
    """A 2-terminal diode's cathode and anode pads, whatever its ratings:
    {k, a, how}; the extraction's K/A pins first, then _cathode_by_pads.
    None for anything that is not a D*/LED* part with two pads."""
    if not re.match(r"^(?:D|LED)\d", b["ref"]) or len(b["pads"]) != 2:
        return None
    if ext:
        idx = pin_index(ext.get("pinout") or [])
        ks, as_ = idx.get("K") or [], idx.get("A") or []
        if ks and as_:
            k, a, how = ks[0]["pin"], as_[0]["pin"], "pinout"
        else:
            k, a, how = _cathode_by_pads(b, netfns)
    else:
        k, a, how = _cathode_by_pads(b, netfns)
    if k not in b["pads"] or a not in b["pads"] or k == a:
        return None
    return {"k": k, "a": a, "how": how}


CLAMP_MPN_RE = re.compile(
    r"^(?:SMBJ|SMAJ|SMCJ|SMDJ|P4SMA|P6SMB|P6KE|1\.5KE|1N47|1N52|1N53|BZX|BZT|"
    r"MMSZ|MM3Z|PESD|ESD|TPD|SP0|SM712|PRTR|USBLC|SRV|RCLAMP|LESD|CDSOD|"
    r"TVS|SMF|SD\d)", re.I)


def clamp_kind(bom: dict | None) -> tuple[str | None, bool]:
    """('TVS'/'Zener'/'ESD' or None, bidirectional) for a diode's parts.json
    row: its Type attribute, else its MPN family; bidirectional from a
    Polarity attribute or a 'CA' suffix on a TVS family MPN."""
    atts = {str(x.get("name") or "").lower(): str(x.get("value") or "")
            for x in (bom or {}).get("attributes") or []}
    mpn = str((bom or {}).get("mpn") or "")
    desc = " ".join([str((bom or {}).get("description") or ""),
                     atts.get("type", ""), atts.get("polarity", "")]).lower()
    kind = None
    t = atts.get("type", "").lower()
    if "tvs" in t:
        kind = "TVS"
    elif "zener" in t or "zener" in desc or any("zener" in n for n in atts):
        kind = "Zener"
    elif "esd" in t:
        kind = "ESD"
    elif CLAMP_MPN_RE.match(mpn):
        kind = "TVS"
    bi = "bidirectional" in desc or "bi-directional" in desc or bool(
        re.match(r"^(?:SMBJ|SMAJ|SMCJ|SMDJ|P4SMA|P6SMB|P6KE|1\.5KE)"
                 r"[\d.]+CA\b", mpn, re.I))
    return kind, bi


def _tvs_text_clamps(ext: dict | None) -> dict:
    """{vbr, vc} (each where printed) from a TVS extraction's free-text
    rows: the top of its V(BR) breakdown row and its VC clamping row."""
    got: dict[str, float] = {}
    for row in (ext or {}).get("abs_max") or []:
        kind, scale = unit_kind(str(row.get("unit") or ""))
        if kind != "voltage":
            continue
        p = str(row.get("param") or "").upper()
        key = "vbr" if re.search(r"\bV\s*\(?BR\)?|BREAKDOWN", p) else \
            "vc" if re.search(r"\bVC\b|CLAMPING VOLTAGE", p) else None
        nums = [float(x) * scale for x in
                re.findall(r"\d+(?:\.\d+)?", str(row.get("value")))]
        if key and nums:
            got[key] = max(got.get(key, 0.0), max(nums))
    return {k: v for k, v in got.items() if v > 0}


def tvs_clamps(b: dict, ext: dict | None, bom: dict | None) -> dict | None:
    """{vbr, vc, src} for a unidirectional TVS: vbr the top of its breakdown
    range (where it may start to conduct, with no surge current: the most a
    rail it guards sits at), vc its clamping voltage at its rated peak pulse
    current (what a surge drives the rail to). Each from, in order: the
    extraction's V(BR)/VC rows, the parts.json Clamping Voltage attribute
    (LCSC's "Voltage - Breakdown" is the range's bottom, so never vbr), then
    its part number's row in diode_vr.yaml (mpn_clamp). None for a part that
    is not a unidirectional TVS or has neither number (#500)."""
    kind, bi = clamp_kind(bom or {"mpn": b.get("mpn") or b.get("value")})
    if kind is None and ext:
        kind, bi = clamp_kind({"mpn": ext.get("mpn")})
    if kind != "TVS" or bi:
        return None
    got = {k: (v, "datasheet extraction")
           for k, v in _tvs_text_clamps(ext).items()}
    for a in (bom or {}).get("attributes") or []:
        if "vc" not in got and re.search(r"clamping voltage",
                                         str(a.get("name") or ""), re.I):
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", str(a.get("value") or ""))
            if m:
                got["vc"] = (float(m.group(1)),
                             f"parts.json {a.get('name')} = {a.get('value')}")
    for pn in (b.get("mpn"), (bom or {}).get("mpn"), b.get("value"),
               (ext or {}).get("mpn")):
        if "vbr" in got and "vc" in got:
            break
        if (row := diode_vr.mpn_clamp(pn or "")) and not row["bi"]:
            src = f"part number {pn}: {row['family']} ({row['cite']})"
            got.setdefault("vbr", (row["vbr"], src))
            got.setdefault("vc", (row["vc"], src))
    if not got:
        return None
    return {"vbr": got.get("vbr", (None,))[0], "vc": got.get("vc", (None,))[0],
            "src": "; ".join(dict.fromkeys(s for _, s in got.values()))}


def rail_clamps(board: list[dict], exts: dict, bom_by_lcsc: dict,
                netfns: dict, volts: dict, vsrc: dict,
                constraints: dict | None) -> dict[str, dict]:
    """{rail: {vbr, vc, by, src}} for each rail of known positive voltage a
    clamp holds (#500): a constraints `voltages` entry's clamp_v (and
    breakdown_v), else the unidirectional TVS (tvs_clamps) whose cathode
    sits on the rail and anode on a 0 V net; with several, the lowest of
    each number. A rail whose clamp is not above its own voltage is left
    out: that is the TVS's own rating, judged as a diode."""
    out: dict[str, dict] = {}
    for b in board:
        if not re.match(r"^D\d", b["ref"]):
            continue
        ext = exts.get(b["lcsc"])
        tvs = tvs_clamps(b, ext, bom_by_lcsc.get(b["lcsc"]))
        dp = diode_pads(b, ext, netfns) if tvs else None
        if not dp:
            continue
        rail, gnd = b["pads"][dp["k"]], b["pads"][dp["a"]]
        v = volts.get(rail)
        if v is None or v <= 0 or vsrc.get(rail) == "pull-up" or not (
                volts.get(gnd) == 0 or is_ground(base_net(gnd))):
            continue
        cur = out.setdefault(rail, {"vbr": None, "vc": None, "by": [],
                                    "src": []})
        for k in ("vbr", "vc"):
            if tvs[k] is not None and (cur[k] is None or tvs[k] < cur[k]):
                cur[k] = tvs[k]
        cur["by"].append(b["ref"])
        cur["src"].append(f"{b['ref']}: {tvs['src']}")
    for e in (constraints or {}).get("voltages") or []:
        net = e.get("net")
        if e.get("clamp_v") is None or volts.get(net) is None:
            continue
        out[net] = {"vbr": None if e.get("breakdown_v") is None
                    else float(e["breakdown_v"]),
                    "vc": float(e["clamp_v"]), "by": ["constraints"],
                    "src": ["constraints voltages clamp_v"]}
    for net in list(out):
        c = out[net]
        if c["vbr"] is not None and c["vc"] is not None and c["vc"] < c["vbr"]:
            c["vc"] = c["vbr"]
        if max(x for x in (c["vbr"], c["vc"], 0.0) if x is not None) \
                <= volts[net] + EPS:
            del out[net]
    return out


def clamp_findings(board: list[dict], exts: dict, bom_by_lcsc: dict,
                   netfns: dict, volts: dict, vsrc: dict,
                   constraints: dict | None, diodes: dict, emit) -> dict:
    """rating_over_clamp (#500): on a rail a TVS clamps, each part's voltage
    rating is judged at the TVS's breakdown max (no clamp current yet) and at
    its clamping voltage (a surge), not at the rail's nominal. Rated: an
    extracted part's pins on the rail by their absolute-max voltage rows
    (from ground, or from a named pin on a known net); an unextracted 2-net
    part by its voltage-rated attribute or a capacitor's voltage code; a
    diode by its reverse rating. The clamps themselves, and a part the
    nominal already breaks (rating_over_voltage says so), are not judged.
    Returns facts: {rail: {vbr, vc, by, src, judged}}."""
    clamps = rail_clamps(board, exts, bom_by_lcsc, netfns, volts, vsrc,
                         constraints)
    if not clamps:
        return {}
    for c in clamps.values():
        c["judged"] = []

    def judge(ref, net, rating, src, vref, nominal, pin, what):
        c = clamps[net]
        if rating <= nominal + EPS:
            return  # the rail itself breaks it: rating_over_voltage
        c["judged"].append(pin or ref)
        for level, top in (("breakdown", c["vbr"]), ("clamp", c["vc"])):
            if top is None or top - vref <= rating + EPS:
                continue
            why = ("before it conducts, with no surge current: its breakdown "
                   "max" if level == "breakdown" else
                   "in a surge, at its rated peak pulse current: its clamping "
                   "voltage")
            emit("rating_over_clamp", "warning", ref, net,
                 f"{what} is rated {_fmt(rating)} V [{src}] on {net} "
                 f"({_fmt(nominal)} V nominal), which {', '.join(c['by'])} "
                 f"lets reach {_fmt(top - vref)} V {why} "
                 f"[{'; '.join(c['src'])}]",
                 pin=pin, net_v=top - vref, limit_v=rating, rating=src,
                 level=level, clamp=c["by"])
            return

    for b in sorted(board, key=lambda x: x["ref"]):
        ref, pads = b["ref"], b["pads"]
        on = sorted({n for n in pads.values() if n in clamps})
        if not on or any(ref in c["by"] for c in clamps.values()):
            continue
        ext = exts.get(b["lcsc"])
        bom = bom_by_lcsc.get(b["lcsc"])
        if re.match(r"^(?:D|U|TVS|ESD)\d", ref) and (
                (clamp_kind(bom)[0] if bom else None)
                or clamp_kind({"mpn": b.get("mpn") or b.get("value")})[0]
                or clamp_kind({"mpn": (ext or {}).get("mpn")})[0]):
            continue  # a clamp in its own right
        if ext is not None:
            ratings = part_ratings(ext)
            pinout = ext.get("pinout") or []
            idx = pin_index(pinout)
            for pin, net in sorted(pads.items()):
                if net not in clamps:
                    continue
                p = next((q for q in pinout if q["pin"] == pin), None)
                if p is None or p["type"] in ("ground", "nc"):
                    continue
                best = None
                for r in ratings.get(pin, {}).get(("voltage", "abs"), []):
                    if not isinstance(r["hi"], float) or r["hi"] <= 0:
                        continue
                    if r["ref"]:
                        vs = [volts.get(pads.get(h["pin"]))
                              for h in idx.get(str(r["ref"]).upper()) or []]
                        vs = [x for x in vs if x is not None]
                        if not vs:
                            continue
                        vref = min(vs)
                    else:
                        vref = 0.0
                    if best is None or r["hi"] + vref < best[0] + best[1]:
                        best = (r["hi"], vref, r["src"])
                if best:
                    judge(ref, net, best[0], best[2], best[1],
                          volts[net] - best[1], f"{ref}.{pin}",
                          f"{ref}.{pin} ({p['name']})")
            continue
        dio = diodes.get(ref)
        if dio:
            k, a = pads[dio["k"]], pads[dio["a"]]
            if k in clamps and volts.get(a) is not None:
                judge(ref, k, dio["vr"], dio["src"], volts[a],
                      volts[k] - volts[a], f"{ref}.{dio['k']}",
                      f"{ref} (reverse, cathode on {k})")
            continue
        nets = sorted(set(pads.values()))
        if len(nets) != 2:
            continue
        rated, src = bom_volts(bom), "parts.json"
        if rated is None and (code := cap_code_rating(b, bom)):
            rated, src = code
        if rated is None:
            continue
        net = on[0]
        other = nets[1] if nets[0] == net else nets[0]
        if other in clamps or volts.get(other) is None:
            continue
        judge(ref, net, rated, src, volts[other], volts[net] - volts[other],
              None, ref)
    return clamps


FWD_MIN = 0.5    # V: a forward bias past this conducts in any silicon diode


def diode_forward_findings(board, bom_by_lcsc, exts, netfns, volts, vsrc,
                           emit) -> None:
    """rating_diode_forward: a diode wired so it conducts all the time. A
    diode whose anode sits on a stated rail and whose cathode is on ground
    shorts the rail through itself; a TVS or Zener clamp whose anode sits
    above its cathode is fitted backwards (it should block until the rail
    passes its breakdown) and clamps the rail at one diode drop. A plain
    diode forward between two rails is an OR-ing or a supply path and is
    left alone, as is a bidirectional clamp and any net whose volts are only
    guessed through a pull-up."""
    for b in sorted(board, key=lambda x: x["ref"]):
        bom = bom_by_lcsc.get(b["lcsc"])
        dio = diode_pads(b, exts.get(b["lcsc"]), netfns)
        if not dio:
            continue
        kind, bi = clamp_kind(bom)
        if bi:
            continue
        nk, na = b["pads"][dio["k"]], b["pads"][dio["a"]]
        vk, va = volts.get(nk), volts.get(na)
        if vk is None or va is None or "pull-up" in (vsrc.get(nk),
                                                      vsrc.get(na)):
            continue
        fwd = va - vk  # not a rating, so not counted in compared()
        if fwd <= FWD_MIN + EPS:
            continue
        if is_ground(nk):
            why = f"the anode is on {na} and the cathode on ground {nk}, " \
                "so it conducts across the rail"
        elif kind in ("TVS", "Zener"):
            why = f"a {kind} clamp should block its rail (cathode to the " \
                "higher net) until breakdown; forward biased it clamps the " \
                "rail at one diode drop"
        else:
            continue
        emit("rating_diode_forward", "error", b["ref"], na,
             f"{b['ref']}{' (' + kind + ')' if kind else ''} is forward "
             f"biased by {_fmt(fwd)} V (anode {na} = {_fmt(va)} V, cathode "
             f"{nk} = {_fmt(vk)} V): {why} - is it fitted backwards? "
             f"[{dio['how']}]", pin=f"{b['ref']}.{dio['a']}", net_v=fwd,
             limit_v=FWD_MIN, rating=dio["how"])


def reversed_input_volts(board: list[dict], volts: dict, vsrc: dict,
                         rail: str) -> dict[str, float]:
    """Net volts with a power input plugged in reversed: its `rail` pin sits
    at minus the rail's volts against the board's ground. A net reached from
    it through one series part (R, L, FB) and touched by no other series
    part takes its volts (a reverse-biased diode passes no current, so the
    resistor drops nothing); every other net is unknown."""
    rev = {n: 0.0 for n in volts if is_ground(n)}
    rev[rail] = -volts[rail]
    series: dict[str, list[str]] = {}
    for b in board:
        nets = sorted(set(b["pads"].values()))
        if SERIES_RE.match(b["ref"]) and len(nets) == 2:
            for x, y in ((nets[0], nets[1]), (nets[1], nets[0])):
                series.setdefault(x, []).append(y)
    grew = True
    while grew:
        grew = False
        for x, ys in series.items():
            if x in rev or len(ys) != 1 or ys[0] not in rev or \
                    vsrc.get(x, "pull-up") != "pull-up":
                continue
            rev[x] = rev[ys[0]]
            grew = True
    return rev


def input_connectors(board: list[dict], volts: dict) -> list[tuple]:
    """(connector, rail net) for each 2-net power input: a J/P/CN/X/BT part
    whose pads sit on one ground net and one positive rail and nothing else -
    a header, jack or battery that can be plugged in the wrong way round."""
    out = []
    for b in board:
        nets = set(b["pads"].values())
        if not INPUT_CONN_RE.match(b["ref"]) or len(nets) != 2:
            continue
        gnd = [n for n in nets if is_ground(n)]
        rails = [n for n in nets if not is_ground(n)
                 and (volts.get(n) or 0) > 0]
        if len(gnd) == 1 and len(rails) == 1:
            out.append((b, rails[0]))
    return out


def _pin_name(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


def pinout_fact(b: dict, ext: dict | None) -> tuple[str, str]:
    """(status, value) for a part's pin map: the board's pad numbers must all
    be pins of the datasheet extraction, and where the pad carries a pin
    name, it must be that pin's name (or an alias of it). A name that
    disagrees is unknown, not failed: the two may spell one pin differently,
    and a reader has to look."""
    if ext is None or not ext.get("pinout"):
        return "unknown", "no datasheet extraction with a pinout"
    pins = {str(p.get("pin")): p for p in ext["pinout"]}
    nums = [n for n in b.get("nums") or [] if n]
    missing = [n for n in nums if n not in pins and n.upper() != "EP"
               and not (n == str(len(pins) + 1)
                        and (ext.get("exposed_pad") or {}).get("present"))]
    if missing:
        return "failed", (f"board pad(s) {', '.join(missing[:8])} are not "
                          f"pins of the {ext.get('mpn')} extraction")
    differ = []
    for n, fn in sorted((b.get("fns") or {}).items()):
        p = pins.get(n)
        if p is None or not fn:
            continue
        a, c = _pin_name(fn), _pin_name(p.get("name"))
        if a and c and not (a in c or c in a or aliases(fn) & aliases(
                str(p.get("name")))):
            differ.append(f"{n}: board {fn} / datasheet {p.get('name')}")
    if differ:
        return "unknown", ("pin names differ, check by eye: "
                           + "; ".join(differ[:6]))
    named = sum(1 for n in nums if (b.get("fns") or {}).get(n))
    moved = b.get("pin_map") or {}
    return "verified", (f"{len(nums)} pads are pins of the extraction"
                        + (f", {named} named alike" if named else "")
                        + (" (matched by place on the land drawing: "
                           + ", ".join(f"pad {k} is pin {v}"
                                       for k, v in list(moved.items())[:6])
                           + ")" if moved else ""))


PDF_NAME_RE = re.compile(r"[^/\\\s]+\.pdf", re.I)
LCSC_CODE_RE = re.compile(r"(?<![A-Za-z0-9])C\d+(?![A-Za-z0-9])")


def extraction_pdf(ext: dict, lcsc: str) -> str:
    """The LCSC code whose PDF an extraction was read from: the one its
    source_pdf file name gives, else its own. source_pdf is free text, so only
    the first PDF's file name counts, not its folders or a note after it."""
    name = PDF_NAME_RE.search(str(ext.get("source_pdf") or ""))
    codes = LCSC_CODE_RE.findall(name.group(0)) if name else []
    return lcsc if not codes or lcsc in codes else codes[0]


# --- a distributor's listing against the datasheet (#799) ---------------------

# how far apart two numbers for one rating may sit: LCSC rounds (5 V for 5.25 V)
LISTING_TOL = 0.10
# what a listing's attribute rates, and the datasheet rows that rate the same
# thing. Supply is not here: its rows are named after pins, and it is judged
# against the whole part (listing_comparisons).
LISTING_FAMILIES = (
    ("drain_source", re.compile(r"drain.*source|\bvds", re.I),
     re.compile(r"\bVDSS?\b|drain.?(to.?)?source", re.I)),
    ("reverse", VR_ATTR_RE,
     re.compile(r"\bV_?(R|RRM|RWM|RM)\b|reverse|stand-?off|blocking", re.I)),
    ("rated", re.compile(V_ATTR_RE.pattern + r"|voltage\s*-\s*max", re.I),
     re.compile(r"rated.?voltage|voltage.?rat(ing|ed)|working voltage", re.I)))
SUPPLY_ATTR_RE = re.compile(r"input\s*voltage|voltage\s*-\s*input|\bvin\b|"
                            r"supply\s*voltage|voltage\s*-\s*supply|"
                            r"operating\s*voltage", re.I)
# a pin_ratings row rated one terminal against another says its family itself
LISTING_PIN_PAIRS = {"reverse": ({"K", "CATHODE"}, {"A", "ANODE"}),
                     "drain_source": ({"D", "DRAIN"}, {"S", "SOURCE"})}


def listing_ratings(bom: dict | None) -> list[dict]:
    """[{rating, attr, v}]: the voltage ratings a parts.json row lists, the top
    of a range. Supply comes first: V_ATTR_RE also takes a supply's (max)."""
    out, seen = [], set()
    for a in (bom or {}).get("attributes") or []:
        name = str(a.get("name") or "")
        vs = attr_volts(a.get("value"))
        if not vs or NOT_VIN_ATTR_RE.search(name):
            continue
        rating = "supply" if SUPPLY_ATTR_RE.search(name) else next(
            (f for f, rx, _ in LISTING_FAMILIES if rx.search(name)), None)
        if rating and (rating, max(vs)) not in seen:
            seen.add((rating, max(vs)))
            out.append({"rating": rating, "attr": name, "v": max(vs)})
    return out


def datasheet_voltage_rows(ext: dict | None) -> list[dict]:
    """[{v, param, pins}]: an extraction's numeric voltage limits, by size (a
    P-channel figure prints negative). A limit given as an expression, a test
    voltage and an AC figure are not ratings to set a listing against."""
    rows = []
    for row in (ext or {}).get("abs_max") or []:
        param = str(row.get("param") or "")
        kind, scale = unit_kind(row.get("unit"))
        if kind != "voltage" or SKIP_RE.search(param.upper()) or \
                re.search(r"\bRMS\b", param, re.I):
            continue
        vs = [abs(x) for x in parse_bounds(row.get("value"), param, scale,
                                           bool(REC_RE.search(param.upper())),
                                           kind) if isinstance(x, float)]
        if vs:
            rows.append({"v": max(vs), "param": param, "pins": set()})
    for r in (ext or {}).get("pin_ratings") or []:
        if r.get("kind", "voltage") != "voltage":
            continue
        ends = [parse_term(str(r[k]), 1.0) for k in ("min", "max")
                if r.get(k) is not None]
        vs = [abs(x) for x in ends if isinstance(x, float)]
        if vs:
            rows.append({"v": max(vs),
                         "param": str(r.get("source") or r.get("pins")),
                         "pins": {str(n).upper() for n in
                                  [*(r.get("pins") or []), r.get("ref")] if n}})
    return rows


def _rates_family(row: dict, family: str, rx: re.Pattern) -> bool:
    pair = LISTING_PIN_PAIRS.get(family)
    return bool(rx.search(row["param"])) or bool(
        pair and row["pins"] & pair[0] and row["pins"] & pair[1])


def listing_comparisons(bom: dict | None, ext: dict | None) -> list[dict]:
    """One dict per listed voltage rating that the datasheet can answer:
    {rating, listing_attr, listing_v, datasheet_row, datasheet_v, agree, side}.

    A datasheet may give one rating several rows (VRM and VR; UL and IEC), so a
    listing that matches any of them agrees, and otherwise it is set against
    the closest. For a supply range or a plain rated voltage with no row of its
    family, all that can be said is that a listing over everything on the part
    is wrong: one under it may be a nominal, or a figure for one pin. A VDS or a
    reverse rating with no row of its own says nothing: a part's other rows are
    other terminals (IRF640: VGS 20 V is no answer to VDS 200 V). side is `high`
    when the listing overstates the part, `low` when it understates it."""
    rows = datasheet_voltage_rows(ext)
    if not rows:
        return []
    top = max(rows, key=lambda r: r["v"])
    res = []
    for li in listing_ratings(bom):
        rx = next((x for f, _, x in LISTING_FAMILIES if f == li["rating"]), None)
        same = [r for r in rows if rx and _rates_family(r, li["rating"], rx)]
        if same:
            near = min(same, key=lambda r: abs(r["v"] - li["v"]))
            agree = abs(near["v"] - li["v"]) <= LISTING_TOL * near["v"]
        elif li["rating"] in LISTING_PIN_PAIRS:
            continue
        else:
            near = top
            agree = li["v"] <= near["v"] * (1 + LISTING_TOL)
        res.append({"rating": li["rating"], "listing_attr": li["attr"],
                    "listing_v": li["v"], "datasheet_row": near["param"],
                    "datasheet_v": near["v"], "agree": agree,
                    "side": None if agree else
                    "high" if li["v"] > near["v"] else "low"})
    return res


def listing_findings(board: list[dict], exts: dict, bom_by_lcsc: dict,
                     read_from: dict, held_back: dict, rated: dict,
                     trusted: set, emit, out: list[dict]
                     ) -> tuple[list[dict], dict]:
    """rating_listing_disagrees, one per part and whatever the rail: the worst
    disagreement, a listing that overstates the part first. A number read from
    a suspect PDF proves nothing, and a person's verified rating has settled it.
    Returns what was compared and what could not be, for the survey."""
    compared_rows, skipped = [], {}
    for lcsc in sorted(exts):
        if not listing_ratings(bom_by_lcsc.get(lcsc)):
            continue
        if read_from.get(lcsc, lcsc) in held_back:
            skipped[lcsc] = "suspect datasheet"
            continue
        comps = listing_comparisons(bom_by_lcsc.get(lcsc), exts[lcsc])
        if not comps:
            skipped[lcsc] = "no voltage row in the extraction"
            continue
        compared_rows += [{"lcsc": lcsc, "rating": c["rating"],
                           "listing_v": c["listing_v"],
                           "datasheet_v": c["datasheet_v"],
                           "agree": c["agree"], "side": c["side"]}
                          for c in comps]
        refs = sorted(b["ref"] for b in board if b["lcsc"] == lcsc)
        bad = sorted((c for c in comps if not c["agree"]),
                     key=lambda c: (c["side"] != "high",
                                    -abs(c["listing_v"] - c["datasheet_v"])))
        if not bad or not refs or set(refs) <= trusted:
            continue
        c = bad[0]
        used = "datasheet" if any("datasheet" in rated.get(r, ())
                                  for r in refs) else "none"
        word = ("the rating check used the datasheet" if used == "datasheet"
                else "the rating check compared neither: no pin of it has a "
                     "datasheet limit on a known rail")
        what = ("the listing overstates the part, so a rail between the two "
                "passes a `listed` rating the datasheet forbids"
                if c["side"] == "high" else
                "the listing understates the part: a rating read from it is "
                "only conservative")
        emit("rating_listing_disagrees", "warning", refs[0], None,
             f"{', '.join(refs)} ({lcsc}): the listing's {c['listing_attr']} "
             f"says {_fmt(c['listing_v'])} V and the datasheet's "
             f"{c['datasheet_row']} says {_fmt(c['datasheet_v'])} V; {what}; "
             f"{word}. Read the PDF and correct whichever is "
             "wrong", pins=refs, listing_v=c["listing_v"],
             datasheet_v=c["datasheet_v"], rating=c["rating"],
             listing_attr=c["listing_attr"], datasheet_row=c["datasheet_row"],
             side=c["side"], check_used=used)
        out[-1]["refs"] = refs
    return compared_rows, skipped


def _rating_wording(tiers: set[str], n: int) -> str:
    pins = f"{n} connected pin(s) within "
    rails = " at the board's rail voltages"
    if tiers == {"datasheet"}:
        return f"{pins}their datasheet rating{rails}"
    if "package" in tiers:
        return (f"{pins}the generic limiting voltage for its package{rails}; "
                "no datasheet or distributor rating")
    if "part_code" in tiers:
        return (f"{pins}the voltage its part number's maker code or its Value "
                f"field states{rails}; no datasheet or distributor rating")
    if "datasheet" in tiers:
        return (f"{pins}their datasheet rating or the distributor's listed "
                f"rating{rails}; not all of it read from a datasheet")
    return f"{pins}the distributor's listed rating{rails}; no datasheet read"


def _rating_source(tiers: set[str], b: dict, src: str,
                   bom: dict | None = None) -> str:
    """Where the numbers of a rating fact came from; src is the extraction's."""
    listing = f"parts.json attributes of {b['lcsc']}"
    if "package" in tiers:
        return _package_src(b.get("footprint"))
    if "part_code" in tiers:
        code = cap_code_rating(b, bom) or (
            (0.0, d["src"]) if (d := diode_code_rating(b, bom)) else None)
        return (code or (0.0, listing))[1]
    if "distributor" not in tiers:
        return src
    return f"{src}; {listing}" if "datasheet" in tiers else listing


# Two-terminal parts rated by a field on their parts row (a voltage), not a
# datasheet's pin table.
PASSIVE_REF = re.compile(r"^(C|R|RN|D|LED|F|FB|L|Y|X|TVS|Z)\d", re.I)


def why_nothing_judged(board: list[dict], exts: dict, vios: list[dict],
                       volts: dict, bom_by_lcsc: dict | None = None
                       ) -> tuple[str, dict[str, list[str]]]:
    """Why a board where no rating met a known voltage judged nothing: each
    part with a non-ground net by the first thing that kept it out (#1330).
    A part with a rating source (a datasheet read, or a field on its parts
    row) that the check didn't call unrated: no net it touches has a known
    voltage. Any other part: no part number, a two-terminal part whose parts
    row has no voltage field, a part number with no datasheet read, or a
    datasheet read whose pins carry no rating it uses. The reason reads
    biggest group first, with its count; the rest follow in brackets."""
    bom_by_lcsc = bom_by_lcsc or {}
    unrated = {r for v in vios if v.get("kind") in
               ("rating_unrated", "rating_no_attributes")
               for r in v.get("refs") or () if r}
    why: dict[str, list[str]] = {}
    for b in sorted(board, key=lambda x: x["ref"]):
        nets = set(b["pads"].values()) - {""}
        if not nets or all(is_ground(n) for n in nets):
            continue
        has_src = b["lcsc"] in exts or bool(
            (bom_by_lcsc.get(b["lcsc"]) or {}).get("attributes"))
        if b["ref"] not in unrated and has_src:
            r = "no known voltage on its nets"
        elif PASSIVE_REF.match(b["ref"]) and len(nets) <= 2:
            r = "passive with no voltage field on its parts row"
        elif not (b["lcsc"] or b.get("mpn")):
            r = "no part number"
        elif b["lcsc"] in exts:
            r = "datasheet read but no pin rating it can use"
        else:
            r = "no datasheet read for its part number"
        why.setdefault(r, []).append(b["ref"])
    if not why:
        return "no connected part on the board", why
    if not any(v for n, v in volts.items() if not is_ground(n)):
        lead = "no net on the board has a known voltage; "
    else:
        lead = ""
    total = sum(len(v) for v in why.values())
    order = sorted(why.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    (top, refs), rest = order[0], order[1:]
    text = f"{lead}{top} ({len(refs)} of {total} parts not compared)"
    if rest:      # bracketed, so the corpus's blocker label is the top reason
        text += " [also " + "; ".join(f"{r}: {len(v)}" for r, v in rest) + "]"
    return text, why


def ledger_facts(board: list[dict], exts: dict, vios: list[dict],
                 checked: dict[str, int], bom_by_lcsc: dict,
                 suspect: dict[str, str] | None = None,
                 rated: dict[str, set[str]] | None = None) -> list[dict]:
    """rating and pinout facts per refdes (lib/ledgerlib.py). rated says where
    each compared number came from (datasheet, distributor, package; with no
    such record, an extraction's or else the distributor's), and only a
    rating read wholly from a datasheet is verified. A suspect PDF holds back
    what was read from it, and nothing else: suspect is keyed by the code
    whose PDF it is (extraction_pdf)."""
    suspect = suspect or {}
    failed: dict[str, str] = {}
    unrated: dict[str, str] = {}
    for v in vios:
        for ref in v.get("refs") or []:
            if v.get("kind") == "rating_no_attributes":
                # never judged, not judged bad
                unrated[ref] = v.get("msg") or ""
            elif v.get("severity") == "error":
                failed.setdefault(ref, v.get("msg") or v.get("message", ""))
            elif v.get("kind") == "rating_unrated":
                unrated.setdefault(ref, (
                    v.get("msg") or "") if len(v.get("refs") or []) == 1
                    else "no datasheet extraction and no voltage-rated "
                         "attribute in parts.json")
    out = []
    for b in board:
        ref = b["ref"]
        ext = exts.get(b["lcsc"])
        src = ((ext or {}).get("source_pdf")
               or (f"parts/{b['lcsc']}.json (datasheet extraction)"
                   if ext else f"parts.json attributes of {b['lcsc']}"))
        tiers = ((rated or {}).get(ref)
                 or {"datasheet" if ext else "distributor"})
        sus = suspect.get(extraction_pdf(ext, b["lcsc"])) if ext else None
        if ref in failed:
            st, val = "failed", failed[ref]
        elif ref in unrated:
            st, val = "unknown", unrated[ref]
        elif checked.get(ref):
            st = "verified" if tiers == {"datasheet"} else "listed"
            val = _rating_wording(tiers, checked[ref])
        else:
            st, val = "unknown", ("no pin of it sits on a net of known "
                                  "voltage" if b["pads"] else "no connected pad")
        if sus and st in ("verified", "listed") and "datasheet" in tiers:
            st, val = "unknown", f"datasheet suspect, not verified: {sus}"
        fact = {"subject": ref, "kind": "rating", "status": st, "value": val,
                "source": "", "lcsc": b["lcsc"]}
        if st != "unknown":
            weakest = next(t for t in ("package", "part_code", "distributor",
                                       "datasheet") if t in tiers)
            fact.update(source=_rating_source(tiers, b, src,
                                              bom_by_lcsc.get(b["lcsc"])),
                        rated_from=weakest)
        out.append(fact)
        pst, pval = pinout_fact(b, ext)
        if sus and pst == "verified":
            pst, pval = "unknown", f"datasheet suspect, not verified: {sus}"
        out.append({"subject": ref, "kind": "pinout", "status": pst,
                    "value": pval, "lcsc": b["lcsc"],
                    "source": src if pst != "unknown" else ""})
    return out


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--parts", help="parts dir (default <ws>/parts)")
    ap.add_argument("--constraints", help="constraints.json (voltages, power)")
    ap.add_argument("--power-tree", help="power_tree.md "
                    "(default <ws>/architecture/power_tree.md)")
    ap.add_argument("--extractions", help="a further folder of <part>.json "
                    "extractions, read after --parts and the library "
                    "(abs_max_batch.py's output)")
    ap.add_argument("--out", help="write the report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    ws = pcb.resolve().parent.parent
    parts = Path(args.parts) if args.parts else ws / "parts"
    if not parts.is_dir():
        raise CheckError(f"parts dir not found: {parts}")
    tree = Path(args.power_tree) if args.power_tree else \
        ws / "architecture" / "power_tree.md"
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else None
    import ledgerlib
    # a fact someone else (a person, `ledger.py set`) recorded verified is
    # evidence this check lacks: it is not re-warned and not overwritten
    # unless the check finds it actually failed (#429)
    held = {k: f for k, f in ledgerlib.load(ws)["facts"].items()
            if f.get("kind") in ("rating", "pinout")
            and f.get("status") == "verified" and f.get("by") != SCRIPT} \
        if ledgerlib.workspace_of(pcb) else {}
    trusted = {f["subject"] for f in held.values() if f["kind"] == "rating"}
    viol, facts = run_check(pcb, parts, cons, tree,
                            Path(args.constraints).resolve().parent
                            if args.constraints else None, trusted=trusted,
                            extractions=Path(args.extractions)
                            if args.extractions else None)
    lf = facts.pop("ledger_facts")
    for f in lf:
        h = held.get(ledgerlib.key(f["subject"], f["kind"]))
        if h is not None and f["status"] != "failed":
            f.update(status="verified", value=h.get("value", ""),
                     source=h.get("source", ""), held_by=h.get("by"))
            f.pop("rated_from", None)
    facts["held_verified"] = sorted(held)
    if ledgerlib.writable(ws):
        exp = ledgerlib.expected(ws)
        led = ledgerlib.put(ws, [f for f in lf if ledgerlib.key(
            f["subject"], f["kind"]) in exp and "held_by" not in f], SCRIPT)
        facts["ledger"] = str(led) if led else None
    facts["ledger_counts"] = {
        k: {st: sum(1 for f in lf if f["kind"] == k and f["status"] == st)
            for st in ledgerlib.STATUSES} for k in ("rating", "pinout")}
    return checklib.report(SCRIPT, pcb, viol, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
