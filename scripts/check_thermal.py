"""check_thermal.py - dissipation vs copper heatsink area (SPEC 6.3, P8).

One concern: a regulator / FET that dumps more heat than its copper can shed.
For each dissipating part named in constraints.json, estimate the junction-to-
ambient rise from the heatsink-net copper area and compare to the allowed rise.

Model (calibrated to datasheet anchors 2026-07; PROGRESS S5). theta_JA falls
off exponentially with copper pour area toward a floor:
    theta_JA(A) = theta_floor + (theta_0 - theta_floor) * exp(-A / tau)
    1 oz / 2-layer : theta_0=174, theta_floor=55, tau=350 mm^2
    2 oz / 4-layer : theta_0=140, theta_floor=45, tau=235 mm^2   (planes spread)
Rise ~= power_w * theta_JA(effective_area), with the area credited only up
to A_SAT = 1 in^2 (645 mm^2): the vendor curves the model is fitted to stop
there. Every number is +/-30%; this is a screen, not a sign-off (JEDEC
JESD51 / TI SLOA122).

Copper does NOT saturate at 1 in^2; the vendor curves flatten because their
test boards are small. An annular copper fin (the exact Bessel solution:
the part's pad hull as a disc of radius r0, the copper weight from the
stackup, h = 10 W/m2K on both faces, ~5 still-air convection + ~5
radiation) heats a sheet over a length 1/m = sqrt(k t / 2h) ~ 26 mm for 1 oz,
and for a 2 mm source falls 92 -> 32 K/W from 645 mm^2 to an infinite sheet,
reaching within 10 % of the sheet only at ~4900 mm^2. The bare fin is steeper
than the calibrated curve (it has no package, FR4 or other surfaces in
series), so it is not used raw: past A_SAT the curve is extended as
    theta_ext(A) = theta_JA(A_SAT) + c * (fin(A) - fin(A_SAT)),
    c = (theta_JA(tau) - theta_JA(A_SAT)) / (fin(tau) - fin(A_SAT)),
the fin scaled to the curve's own slope between tau and A_SAT. For 1 oz
2-layer that gives ~64 C/W at 1000 mm^2, ~55 at 2000 and ~50 for an unbounded
pour (vs the screen's 74 everywhere past 645), and a "saturation area"
(within 10 % of the sheet) of ~2100 mm^2 (a 26 mm radius), nearly the same
for any pad size. theta_ext is ADVICE only: pass/fail still uses the clamped screen,
which errs high, since nothing past 645 mm^2 has been checked against a
measurement. What it changes is the remedy: a part that fails on saturated
copper is told to grow the pour (to the saturation area, within its radius)
when theta_ext there would meet the budget, instead of "more copper won't
help". The facts carry copper_sat_mm2, area_far_mm2 (the net's copper within
the saturation radius), theta_spread_cw (theta_ext of that copper) and
theta_sheet_cw (theta_ext of an unbounded pour); area_far is the footprint
of the net's copper on all layers (not their sum: the copper weight carries
the layers). 4-layer boards use the same
extension on MODEL_ML, with the copper thickness summed over the layers (the
planes tied together under the part); that is rougher still.

2-layer boards: the area curve alone floors at theta(A_SAT) ~74 C/W however
the layout is built, so a part with a thermal-via array into a back pour
(TI SLMA002 PowerPAD practice) could never meet a tight budget. The 2-layer
estimate is therefore the better of the area screen and a two-path network:
    top path : theta_top = theta_JA(A_top)  - the pour connected to the part's
               pads on its own side, through the same calibrated curve
    via path : R_vias + theta_JA(A_bot)     - the thermal vias / plated pad
               drills of the net under the part in parallel, into the back
               pour they land in, through the same curve again
    theta_2L = min(theta_JA(A_top + A_bot),  1 / (1/theta_top + 1/via_path))
One via conducts along its plated barrel only: R = t_board / (k_Cu * A_barrel),
A_barrel = pi*((d/2 + t_p)^2 - (d/2)^2), k_Cu = 380 W/m-K, t_p = 18 um (under
IPC-6012 class 2's 20 um average), unfilled (solder wicking ignored) - e.g.
~230 C/W for a 0.3 mm drill in 1.6 mm FR4; FR4 between vias is ignored. The
array is capped at VIA_BENEFIT_CAP. Conservative on purpose: re-using the
whole curve (package and convection included) on each path, and ignoring
solder fill and FR4 conduction, overstates theta. Example: 16 x 0.3 mm vias,
~120 mm2 connected top + ~640 mm2 bottom pour -> ~54 C/W (TPA3118D2 HTSSOP),
against TI's 22 C/W measured on its 2-layer EVM (SLOS708G 6.4). A bare pad with no vias gets the
area screen unchanged. 4-layer boards keep the area screen (it already sums
the inner planes), read a second time on the part's own-side copper when
too few vias reach those planes (below). Via resistance and copper weight on
4 layers are NOT credited: the barrel network above needs a back pour that
convects through its own calibrated curve, and an inner plane has no such
curve - re-running MODEL_ML on each plane would count the planes twice, since
that curve already includes them. The one cited 4-layer via anchor found (TI SCBA017D Fig. 15:
a QFN's theta_JA falls 46.8 -> 29.9 C/W from JESD51-7 to JESD51-5, i.e. with
vias into the planes) is for a 2 mm logic pad and has not been checked
against a power package, so it is not applied here.

The via path needs a back pour to land in: when the back-side copper of the
net that those vias touch is smaller than the part's own pad hull (the vias
end on their own lands, or on a trace stub), the path is dropped and the part
gets the top pour alone. The threshold is a judgement, not a cited figure:
SLMA002 lands its 2-layer via arrays in a bottom-side copper area, and the
curve's theta_JA(0) = 174 C/W stands for a package on its pads, not for a
few via rings, so crediting that would be optimistic.

When a 2-layer part still fails, the error compares the budget against the
screen's best 2-layer case: VIA_BENEFIT_CAP vias (0.3 mm, SLMA002's drill, or
the largest drill found) into saturated pours on both sides, ~38 C/W on
1.6 mm FR4. A part over budget even there gets "cut the dissipation, add a
heatsink, or move to 4 layers" instead of "add thermal vias", because more
vias can't fix it within this model.

On 4+ layers the area screen sums the net's copper on every layer, so it
reads the same with six vias under a pad or none, though only vias carry
the heat into the inner planes. A copper-model part (no rating, datasheet
theta, slug or heatsink) is credited with the planes when at least
VIA_CREDIT_MIN vias of the net (or plated holes) sit near it, or as many as
its min_vias when that is lower (but at least one). Short of that, the
screen also reads the part on its own side's copper alone: the pieces its
pads touch within reach, through MODEL_ML. The truth lies between the two
readings. A rise over budget with the planes credited is an error, as
before, and its message adds the own-side rise. A rise within budget there
but over it on the own side is not judged: the report's not_judged names
the part and the vias that would settle it. A part within budget on both
passes. The facts carry both readings (planes_credited, area_side_mm2,
theta_side_cw, rise_side_c, power_limit_side_w); theta_ja, rise_c and
power_limit_w stay the planes-credited figures. Neither reading is right:
a part with no via still heats the planes through the dielectric (JESD51-7's
2s2p board has its planes and no vias, and SCBA017D's QFN reads 46.8 C/W
there against 29.9 with vias on JESD51-5), so the own-side figure errs high
and the credited one low. VIA_CREDIT_MIN is a judgement: four 0.3 mm vias
to a plane 0.1-0.2 mm down are ~4-7 C/W in series, inside the screen's
+/-30 % of its 51 C/W floor, while a single one is ~15-30 C/W.
Past that credit the reading doesn't count vias (four read as sixteen), so a
copper-model part over budget on saturated copper (or under the copper floor)
isn't told to add vias, which a re-run wouldn't confirm: its error says the
screen has no via lever there and names what would move the reading (cut the
loss, the part's datasheet theta_JA, a heatsink or forced air) (#1181).

Thermal vias. An entry's min_vias is the datasheet's floor for the part's
via array (a PMIC datasheet's 9-via minimum under its exposed pad). It holds on
every part, whatever the copper estimate says: fewer vias near the part is
an error (thermal_vias, explicit), and min_vias 0 asks none. Without one,
the screen computes an array (one via per VIA_PITCH_MM square of the pad
hull, 4 to VIA_BENEFIT_CAP) and asks for it, as a warning, only when the
budget is below the copper's floor, theta_JA(A_SAT) (no rated, pad-up or
heatsinked part is asked). The count is the net's vias and plated pad
drills within sqrt(pad hull area / pi) + 1.5 mm (2 mm at least) of the
pads' centroid.

A part rated by its own datasheet for self-heating (an inductor's "Irms:
current for a 40 C rise", typically) is judged on that rating, not on the
board copper: the area curve is an IC-package curve (theta_JA(0) = 140-174
C/W for a part on its pads), and it says nothing about a 10 mm wound body
that sheds its heat from its own surface. The rating gives the part's own
theta, rise_c / (current_a^2 * dcr_ohm), and the entry's power_w (copper AND
core loss) rides on it. DCR is the 25 C value; at the rated rise the copper
is ~16 % more resistive, so the real rating-point loss is higher and this
theta errs high (conservative). Example: Coilcraft XAL1010-222, 2.8 mOhm,
24.5 A for 40 C -> 23.8 C/W, so 1.1 W -> 26 C (the area curve said 58 C/W).

A part may instead carry its datasheet theta_JA (theta_ja_c_w, with a
required theta_source naming the datasheet and its table/page). That figure
is measured on a JEDEC 2s2p board (JESD51-7: 76 x 114 mm, two solid inner
planes, the part alone dissipating), so it is used AS-IS: it replaces the
copper-area curve and the 2-layer via network, and is never combined with
area or via credit. It is optimistic on a crowded board whose other parts
heat the same copper, and on a layout without the vendor's thermal vias;
for that reason an entry's min_vias still holds on it, and the computed
array is still asked against the copper floor. It is not read a second
time on its own-side copper: that bound is the copper model's alone.
A part takes a rating or a theta_ja_c_w, not both.

Forced air: an entry may declare the airflow over the part (airflow_lfm, or
airflow_m_s at 196.85 LFM per m/s; the board must say it has a fan). theta
is multiplied by the airflow factor, the LEAST improvement among TI's
modeled QFN tables (SCBA017D, Feb 2004, Tables 3-5, theta_JA C/W at 0 / 150
/ 250 / 500 LFM):
    20-pin JESD51-5  29.9 23.1 21.2 19.5     JESD51-7  46.8 40.5 38.2 36.0
    16-pin JESD51-5  31.2 24.4 22.5 20.7     JESD51-7  49.6 42.4 40.1 37.8
    14-pin JESD51-5  31.6 25.1 23.1 21.4     JESD51-7  52.5 46.0 42.9 40.5
The worst ratio at each speed is the 14-pin JESD51-7 row: x0.876 at 150,
x0.817 at 250, x0.771 at 500 LFM. Between points the factor is linear in
LFM (real convection gains are front-loaded, h ~ sqrt(v), so linear errs
high); past 500 LFM it stays at x0.771 (no extrapolation). The same factor
applies to a rated part and to a datasheet theta_JA (JEDEC publishes
forced-air theta_JA on the same 2s2p board). No airflow declared = factor 1.0 = still air,
exactly the model without it.

The corpus carries no thermal constraints, so this check is clean on all
goldens; supply parts via constraints.json to exercise it (a synthetic fixture
is tested).

Pad-up packages (a "top slug": TI's PowerPAD-up DDV/DAD/DKD/DKQ, e.g. the
TPA3221DDV and TPA3116D2DAD) put their exposed pad on TOP, for a heatsink.
The board reaches the die only through the leads (TPA3221: RthetaJB 14.9
C/W, SLASEE9C 6.4), so thermal vias can't drain it and no via array is asked
of it; more copper or vias barely move it. The check knows one by its Value,
LCSC number or MPN (SLUG_TOP_PARTS, SLUG_TOP_LCSC, or a TI TPA/TAS part
number ending in one of those package codes), or by the entry's
"slug": "top". Its board path is the datasheet's no-heatsink theta_JA
(theta_ja_c_w, or the table's figure): without one the entry is an error,
since the copper curve assumes a pad soldered down. A pad-up part on the
board with no thermal entry is a warning: nothing says how its slug is
cooled.

A heatsink (entry "heatsink", any package) is a second path in parallel
with the board path:
    R_hs   = theta_jc (slug side) + interface + spreading + theta_sa
    theta  = 1 / (1/theta_board + 1/R_hs)
theta_jc is the table's RthetaJC(top) for a known pad-up part, else
required. The interface is interface_c_w, or interface_mm / interface_k_w_mk
over contact_mm2 (the slug's area: the table's, else required): a 0.25 mm,
1 W/mK adhesive tape over the TPA3221's 29 mm2 slug is 8.6 C/W, a clamped
0.1 mm 3 W/mK pad 1.1 C/W, which is why the tape alone fails it. theta_sa
(the heatsink's own, in the airflow it will see) needs a source. The
heatsink has to exist and be held: "part" (what to buy) or "ref" (its
footprint on this board), and "hold": clip / screw / pushpin need the
board's provision for it in "mount_refs" (refdes of the holes or slots, on
the board); adhesive and lid need nothing on the board.

theta_ja_c_w is a datasheet's no-heatsink JEDEC figure, so a figure that
already assumes a heatsink is the gate passing a part on copper it doesn't
have. It is an error when: the part is a known one and the figure is under
90 % of the table's no-heatsink theta_JA (the table's figure is then used);
the figure is under 10 C/W with no heatsink declared (no SMD package gets
there on a JEDEC board alone; a judgement, not a cited figure); or its
theta_source speaks of a heatsink that the entry doesn't declare.

The box (constraints "enclosure") sets the air the parts sit in. Its heat
is every thermal entry's power_w plus other_w; it leaves through the walls
and through a vent path by the stack effect:
    G_walls = A_walls / (1/h_in + t_wall/k_wall + 1/h_out),
              h_in 5, h_out 10 W/m2K (still air; outside includes radiation)
    Q       = Cd * A_vent * sqrt(2 g H dT / T),  Cd 0.6 (sharp orifice)
    P       = G_walls * dT + rho * cp * Q(dT) * dT    solved for dT
A_walls is the box's outer surface less its floor ("floor": "feet", the
default, counts half; "flat" none; "open" all). A_vent is the vent path's
inlet, riser and outlet areas in series (1/A^2 = sum 1/A_i^2): a floor vent
under a board that fills the box only breathes through the edge gap, which
is the riser. H is stack_mm (default the box height). box_mm, or the
bounding box of the STL(s) in "stl", sizes the box. Air = room_c (default
30 C) + dT. Every part's junction is then air + rise, and an entry's
tj_max_c sets its allowed rise to tj_max_c - air (dt_c stays a rise over
whatever air there is). The plastic is checked against its glass
transition (PLASTIC_TG_C, or tg_c): air within 10 K of Tg is an error (the
box creeps under its screws), within 20 K a warning. Every term is +/-30 %:
a screen for the box, not a CFD run. Without an enclosure, ambient_c (top
level) is the air if given.

Governor: an entry's power_max_w is the part's continuous worst case (full
power into the lowest load). When that runs past the budget the part needs
a limit in firmware, named in "governor"; without one it is a warning. The
facts carry power_limit_w = allowed rise / theta, the number that limit
needs. On 4+ layers with the planes not credited (above), a maximum within
the budget with them but over it on the part's own-side copper is not
judged (rise_max_side_c, power_limit_side_w give that reading).

Bulk capacitors (constraints "ripple"): each entry's irms_a (operating
point) against its rating, rated_irms_a x freq_multiplier (the datasheet's
frequency factor at the switching frequency), with its source. Over the
rating is an error, over 75 % a warning (a judgement). irms_max_a (full
power) over the rating is a warning. With life_h at rated_c, life at the
box air is life_h x 2^((rated_c - T)/10) (electrolytic) or
10^((rated_c - T)/20) ("chem": "polymer"), where T = air + 5 K x
(irms_a / rating)^2 (a cap's core rise at its full rating is ~5 K, typical
of vendors' ripple ratings; a judgement); under life_min_h is a warning.

Known power ICs with no thermal entry (#884). A board arrives with no
constraints.json, and a motor driver or regulator on thin copper still
overheats (minuet v3's MCF8316D: over 50 C at 4 A per phase,
over-temperature at 4.5 A). A part that reference/thermal_parts.yaml matches
by Value / MPN, and that no thermal entry names, is screened on its own
datasheet: the copper on its biggest pad's net (the exposed pad) through the
model above at 1 W, the most favourable of the screen and the spreading
theta (an inferred screen should fire only when even the kind reading
fails), against the row's JEDEC theta_JA. Over THETA_RATIO_MAX (2x) is a
warning: the part sheds under half the heat the board its ratings assume
sheds, so it reaches its thermal limits at under half the loss. The 2x is a
judgement: minuet v3 (2-layer, 58 C/W against 25.7) fails it, v4 (4-layer
pours, the designer's fix, 42 C/W) passes, and so does the best 2-layer
case (full pours, a via array). Where the row has tj_warn_c and a loss
model, the finding gives the current at which the die reaches its thermal
warning from INFERRED_AIR_C (25 C, JEDEC still air) or ambient_c, here and
on the datasheet's board. Give the part a thermal entry with this board's
load to replace the screen.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     exit 0/1/2 per SPEC section 6.

constraints.json["thermal"] entries:
    {"ref": "U2",           # dissipating part refdes (its pads locate it)
     "power_w": 0.6,        # estimated dissipation
     "net": "GND",          # heatsink net (thermal-pad / tab net)
     "dt_c": 40,            # allowed junction-to-ambient rise (default 40)
     "min_vias": 9,         # optional: the datasheet's thermal-via
                            #   floor, held as an error (0 asks none)
     "airflow_lfm": 200,    # optional forced air over the part (or
                            #   "airflow_m_s"; never both)
     "rating": {"rise_c": 40, "current_a": 24.5, "dcr_ohm": 0.0028,
                "source": "Coilcraft XAL1010 datasheet, Irms 40 C rise"}}
                            # optional datasheet self-heating rating;
                            #   source is required
     "theta_ja_c_w": 29,    # OR the datasheet JEDEC theta_JA, used as-is,
     "theta_source": "LMG2100R044 SNOSDF9B 5.4 Thermal Information"}
                            #   with its required source (not with rating)
     "slug": "top",         # optional: the exposed pad faces up (pad-up)
     "heatsink": {"theta_sa_c_w": 20, "source": "...", "part": "19x12x25",
                  "theta_jc_c_w": 1.1, "interface_mm": 0.2,
                  "interface_k_w_mk": 3, "contact_mm2": 29,
                  "spreading_c_w": 1, "hold": "clip", "mount_refs": "H5,H6"},
     "tj_max_c": 110,       # optional: absolute limit, against the box air
     "power_max_w": 22,     # optional: continuous worst case
     "governor": "..."}     #   and the firmware limit that bounds it
constraints.json["enclosure"] = {"box_mm": [106, 96, 46.8] | "stl": [...],
     "wall_mm": 2.4, "material": "PETG", "room_c": 30, "other_w": 1.4,
     "inlet_mm2": 3300, "riser_mm2": 228, "outlet_mm2": 3300,
     "stack_mm": 40, "floor": "feet"}
constraints.json["ripple"] = [{"ref": "C5", "irms_a": 0.55,
     "rated_irms_a": 0.6, "freq_multiplier": 1.2, "source": "...",
     "irms_max_a": 1.25, "life_h": 2000, "rated_c": 105,
     "life_min_h": 30000, "chem": "electrolytic"}]
"""
from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from pathlib import Path

import yaml
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from checklib import CheckError, violation  # noqa: E402

SCRIPT = "check_thermal"
A_SAT_MM2 = 645.0             # ~1 in^2: copper pour saturates here
MODEL_2L = (174.0, 55.0, 350.0)   # theta_0, theta_floor, tau
MODEL_ML = (140.0, 45.0, 235.0)
DEFAULT_DT_C = 40.0
VIA_PITCH_MM = 1.1           # recommended thermal-via pitch
VIA_BENEFIT_CAP = 36         # array benefit flattens past ~36 vias
VIA_CREDIT_MIN = 4           # 4+ layers: vias near the part before its planes count (module doc)
REACH_MM = (A_SAT_MM2 / math.pi) ** 0.5   # ~14.3 mm: heat spreads only so far
K_CU_W_MK = 380.0            # copper thermal conductivity
H_SPREAD_W_M2K = 10.0        # per face, still air: ~5 convection + ~5 radiation
SPREAD_WITHIN = 0.10         # "saturated": within 10 % of an unbounded sheet
SHEET_R_MM = 2000.0          # an unbounded sheet, for the fin model
DEFAULT_CU_MM = 0.035        # 1 oz, when the stackup names no copper
PLATING_MM = 0.018           # via barrel plating (IPC-6012 cl.2 avg is 0.020)
DEFAULT_BOARD_MM = 1.6
BEST_VIA_DRILL_MM = 0.3      # SLMA002's recommended thermal-via drill
LFM_PER_M_S = 196.85
# TI SCBA017D Tables 3-5: modeled theta_JA (C/W) at AIRFLOW_LFM (module doc)
AIRFLOW_LFM = (0.0, 150.0, 250.0, 500.0)
AIRFLOW_ANCHORS = {
    "QFN-20 JESD51-5": (29.9, 23.1, 21.2, 19.5),
    "QFN-16 JESD51-5": (31.2, 24.4, 22.5, 20.7),
    "QFN-14 JESD51-5": (31.6, 25.1, 23.1, 21.4),
    "QFN-20 JESD51-7": (46.8, 40.5, 38.2, 36.0),
    "QFN-16 JESD51-7": (49.6, 42.4, 40.1, 37.8),
    "QFN-14 JESD51-7": (52.5, 46.0, 42.9, 40.5),
}
# least improvement at each speed across the anchors (conservative)
AIRFLOW_FACTORS = tuple(max(t[i] / t[0] for t in AIRFLOW_ANCHORS.values())
                        for i in range(len(AIRFLOW_LFM)))


# Pad-up ("PowerPAD up") parts by Value / MPN prefix. Thermal numbers only
# where a datasheet gave them; the others still get pad-up handling and
# must carry their own theta_ja_c_w and heatsink theta_jc_c_w.
SLUG_TOP_PARTS = {
    "TPA3221": {"package": "DDV (HTSSOP-44, PowerPAD up)",
                "theta_ja_c_w": 44.8, "theta_jc_top_c_w": 1.1,
                "slug_mm2": 29.0,
                "source": "TI SLASEE9C 5 (heat slug on top), 6.4 (RthetaJA "
                          "44.8, RthetaJC(top) 1.1 C/W), DDV0044D (slug "
                          "6.72-7.30 x 3.85-4.43 mm)"},
    "TPA3116D2": {"package": "DAD (HTSSOP-32, PowerPAD up)",
                  "source": "TI SLOS708G (TPA3116D2: DAD only)"},
    "TPA3255": {"package": "DDV / DKQ (PowerPAD up)",
                "source": "TI TPA3255 datasheet (DDV, DKQ: pad up)"},
}
SLUG_TOP_LCSC = {"C475500": "TPA3221"}      # TPA3221DDVR
# a TI amplifier part number ending in a PowerPAD-up package code
SLUG_TOP_MPN = re.compile(r"^(TPA|TAS)\d{4}[A-Z0-9]*?(DDV|DAD|DKD|DKQ)R?$")
MPN_PROPS = ("MPN", "Manufacturer Part", "Manufacturer_Part_Number",
             "MFR Part", "Part Number", "mpn")
PADUP_THETA_RATIO = 0.9      # a theta_ja_c_w under 90 % of the table's: error
NO_HEATSINK_FLOOR_CW = 10.0  # no SMD package's JEDEC theta_JA is this low
HOLDS = {"clip", "screw", "pushpin", "adhesive", "lid"}
# power ICs screened without a thermal entry (#884, module doc)
THERMAL_PARTS_YAML = (Path(__file__).resolve().parent.parent / "reference"
                      / "thermal_parts.yaml")
THETA_RATIO_MAX = 2.0        # board copper vs the datasheet's JEDEC theta_JA
INFERRED_AIR_C = 25.0        # JEDEC still air, when constraints give none
HOLDS_ON_BOARD = {"clip", "screw", "pushpin"}

# enclosure model (module doc)
H_IN, H_OUT = 5.0, 10.0      # W/m2K inside / outside the box, still air
CD_VENT = 0.6
RHO_CP = 1.2 * 1005.0        # J/m3K, air
G_ACC = 9.81
DEFAULT_ROOM_C = 30.0
PLASTIC_K = 0.2              # W/mK, printed PETG/PLA/ABS (~0.15-0.25)
PLASTIC_TG_C = {"PLA": 60.0, "PETG": 80.0, "PCTG": 80.0, "ABS": 105.0,
                "ASA": 100.0, "PC": 145.0}
TG_ERROR_K, TG_WARN_K = 10.0, 20.0
FLOOR_SHARE = {"feet": 0.5, "flat": 0.0, "open": 1.0}

# bulk-cap ripple (module doc)
RIPPLE_WARN = 0.75
CAP_CORE_RISE_K = 5.0


def airflow_factor(lfm: float) -> float:
    """theta multiplier for forced air at `lfm`; 1.0 in still air."""
    if lfm < 0:
        raise CheckError(f"airflow must be >= 0 LFM, got {lfm}")
    pts = list(zip(AIRFLOW_LFM, AIRFLOW_FACTORS))
    for (x0, f0), (x1, f1) in zip(pts, pts[1:]):
        if lfm <= x1:
            return f0 + (f1 - f0) * (lfm - x0) / (x1 - x0)
    return AIRFLOW_FACTORS[-1]


def entry_airflow_lfm(entry: dict) -> float:
    """The entry's declared forced air in LFM (0 = still air)."""
    lfm, ms = entry.get("airflow_lfm"), entry.get("airflow_m_s")
    if lfm is not None and ms is not None:
        raise CheckError(f"thermal {entry.get('ref')}: give airflow_lfm or "
                         "airflow_m_s, not both")
    try:
        if lfm is not None:
            return float(lfm)
        return float(ms) * LFM_PER_M_S if ms is not None else 0.0
    except (TypeError, ValueError):
        raise CheckError(f"thermal {entry.get('ref')}: airflow must be a "
                         f"number: {lfm if lfm is not None else ms!r}") from None


def rated_theta_cw(entry: dict) -> float:
    """The part's own theta from its datasheet self-heating rating."""
    r = entry["rating"]
    ref = entry.get("ref")
    if not isinstance(r, dict) or not str(r.get("source", "")).strip():
        raise CheckError(f"thermal {ref}: rating needs rise_c, current_a, "
                         "dcr_ohm and a source")
    try:
        rise, amps, dcr = (float(r[k]) for k in
                           ("rise_c", "current_a", "dcr_ohm"))
    except (KeyError, TypeError, ValueError):
        raise CheckError(f"thermal {ref}: rating needs numeric rise_c, "
                         f"current_a and dcr_ohm: {r}") from None
    if min(rise, amps, dcr) <= 0:
        raise CheckError(f"thermal {ref}: rating values must be > 0: {r}")
    return rise / (amps ** 2 * dcr)


def datasheet_theta_cw(entry: dict) -> float:
    """The datasheet's JEDEC theta_JA for the part, validated."""
    ref = entry.get("ref")
    if "rating" in entry:
        raise CheckError(f"thermal {ref}: give rating or theta_ja_c_w, "
                         "not both")
    if not str(entry.get("theta_source", "")).strip():
        raise CheckError(f"thermal {ref}: theta_ja_c_w needs a theta_source "
                         "(datasheet and table/page)")
    try:
        theta = float(entry["theta_ja_c_w"])
    except (TypeError, ValueError):
        raise CheckError(f"thermal {ref}: theta_ja_c_w must be a number: "
                         f"{entry['theta_ja_c_w']!r}") from None
    if not theta > 0:
        raise CheckError(f"thermal {ref}: theta_ja_c_w must be > 0: {theta}")
    return theta


def _num(entry: dict, key: str, where: str, *, positive=True,
         default=None) -> float:
    """entry[key] as a float (> 0 unless positive=False), or default."""
    if key not in entry:
        if default is None:
            raise CheckError(f"{where}: needs {key}")
        return default
    try:
        v = float(entry[key])
    except (TypeError, ValueError):
        raise CheckError(f"{where}: {key} must be a number: "
                         f"{entry[key]!r}") from None
    if positive and not v > 0:
        raise CheckError(f"{where}: {key} must be > 0: {v}")
    return v


def slug_top_info(bg: geom.BoardGeom, ref: str, entry: dict | None = None):
    """What is known of `ref` as a pad-up (top-slug) package, or None.
    The entry's "slug" ("top" / "bottom") overrides what the board says."""
    slug = (entry or {}).get("slug")
    if slug not in (None, "top", "bottom"):
        raise CheckError(f"thermal {ref}: slug must be 'top' or 'bottom'")
    if slug == "bottom":
        return None
    fp = bg.footprints.get(ref) or {}
    props = fp.get("props") or {}
    names = [str(s).strip().upper() for s in
             [fp.get("value")] + [props.get(k) for k in MPN_PROPS] if s]
    lcsc = str(props.get("LCSC", "")).strip().upper()
    if lcsc in SLUG_TOP_LCSC:
        names.insert(0, SLUG_TOP_LCSC[lcsc])
    for name in names:
        for part, info in SLUG_TOP_PARTS.items():
            rest = name[len(part):] if name.startswith(part) else None
            if rest is not None and (rest == "" or
                                     re.fullmatch(r"(DDV|DAD|DKD|DKQ)R?",
                                                  rest)):
                return {"part": part, **info}
        if SLUG_TOP_MPN.match(name):
            return {"part": name, "package": "PowerPAD up",
                    "source": "TI package code (DDV/DAD/DKD/DKQ: pad up)"}
    if slug == "top":
        return {"part": fp.get("value") or ref, "package": "slug on top",
                "source": "constraints entry"}
    return None


def theta_sanity(entry: dict, slug, theta: float, has_hs: bool):
    """Why a theta_ja_c_w can't be a no-heatsink figure, or None; and the
    theta to use instead (module doc)."""
    ref = entry.get("ref")
    known = (slug or {}).get("theta_ja_c_w")
    src = str(entry.get("theta_source", ""))
    if known and theta < PADUP_THETA_RATIO * known:
        return (f"{ref}: theta_ja_c_w {theta:g} C/W is below {slug['part']}'s "
                f"no-heatsink theta_JA ({known:g} C/W, {slug['source']}); a "
                "figure that low assumes a heatsink: give the no-heatsink "
                "figure and declare the heatsink under heatsink"), known
    if has_hs:
        return None, theta
    if theta < NO_HEATSINK_FLOOR_CW:
        return (f"{ref}: theta_ja_c_w {theta:g} C/W is lower than any SMD "
                f"package reaches on a board alone (< "
                f"{NO_HEATSINK_FLOOR_CW:g} C/W) and no heatsink is declared"
                ), theta
    if re.search(r"heat[\s-]?sink", src, re.I):
        return (f"{ref}: theta_source assumes a heatsink ({src[:80]}) but "
                "the entry declares none: declare it under heatsink, with "
                "its part and how it is held, and give the no-heatsink "
                "theta_ja_c_w"), theta
    return None, theta


def heatsink_path(bg: geom.BoardGeom, entry: dict, slug):
    """(R_hs junction-to-air through the heatsink, facts, violations)."""
    ref = entry.get("ref")
    hs = entry["heatsink"]
    where = f"thermal {ref} heatsink"
    if not isinstance(hs, dict):
        raise CheckError(f"{where}: must be an object")
    if not str(hs.get("source", "")).strip():
        raise CheckError(f"{where}: theta_sa_c_w needs a source")
    sa = _num(hs, "theta_sa_c_w", where)
    jc = _num(hs, "theta_jc_c_w", where,
              default=(slug or {}).get("theta_jc_top_c_w"))
    if "interface_c_w" in hs:
        tim = _num(hs, "interface_c_w", where, positive=False)
    else:
        mm = _num(hs, "interface_mm", where)
        k = _num(hs, "interface_k_w_mk", where)
        area = _num(hs, "contact_mm2", where,
                    default=(slug or {}).get("slug_mm2"))
        tim = (mm * 1e-3) / (k * area * 1e-6)
    spread = _num(hs, "spreading_c_w", where, positive=False, default=0.0)
    r_hs = jc + tim + spread + sa
    violations = []
    hold = hs.get("hold")
    if hold not in HOLDS:
        raise CheckError(f"{where}: hold must be one of {sorted(HOLDS)}")
    hs_ref = hs.get("ref")
    if hs_ref and hs_ref not in bg.footprints:
        violations.append(f"its heatsink {hs_ref} is not on the board")
    if not hs_ref and not str(hs.get("part", "")).strip():
        raise CheckError(f"{where}: needs part (what to fit) or ref (its "
                         "footprint on the board)")
    mounts = [m.strip() for m in str(hs.get("mount_refs", "")).split(",")
              if m.strip()]
    if hold in HOLDS_ON_BOARD:
        missing = [m for m in mounts if m not in bg.footprints]
        if not mounts or missing:
            violations.append(
                f"its heatsink is held by {hold} but the board has no "
                f"provision for it ({'mount_refs ' + ', '.join(missing) + ' not on the board' if missing else 'no mount_refs'}): "
                "add the holes or slots, or say how else it is held")
    facts = {"heatsink_r_cw": checklib.rnd(r_hs),
             "heatsink_theta_jc_cw": checklib.rnd(jc),
             "heatsink_interface_cw": checklib.rnd(tim),
             "heatsink_theta_sa_cw": checklib.rnd(sa),
             "heatsink_hold": hold}
    return r_hs, facts, violations


def stl_bbox(paths: list[Path]) -> list[float]:
    """[dx, dy, dz] of the union bounding box of the STL files (mm)."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3

    def take(x, y, z):
        for i, v in enumerate((x, y, z)):
            lo[i], hi[i] = min(lo[i], v), max(hi[i], v)

    for p in paths:
        data = Path(p).read_bytes()
        n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
        if n >= 0 and len(data) == 84 + 50 * n:
            for i in range(n):
                v = struct.unpack_from("<12f", data, 84 + 50 * i)
                for j in (3, 6, 9):
                    take(*v[j:j + 3])
        else:
            for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
                take(*(float(g) for g in m.groups()))
    if not all(math.isfinite(v) for v in lo + hi):
        raise CheckError(f"enclosure stl has no triangles: {paths}")
    return [h - l for l, h in zip(lo, hi)]


def enclosure_air(enc: dict, heat_w: float, base: Path):
    """(air_c, facts, violations) for the box (module doc)."""
    where = "enclosure"
    if not isinstance(enc, dict):
        raise CheckError("enclosure must be an object")
    if "box_mm" in enc:
        dims = enc["box_mm"]
        if not (isinstance(dims, list) and len(dims) == 3
                and all(isinstance(d, (int, float)) and d > 0 for d in dims)):
            raise CheckError(f"{where}: box_mm must be [x, y, z] mm")
        dims = [float(d) for d in dims]
    elif "stl" in enc:
        stl = enc["stl"] if isinstance(enc["stl"], list) else [enc["stl"]]
        dims = stl_bbox([base / s for s in stl])
    else:
        raise CheckError(f"{where}: needs box_mm or stl")
    x, y, z = (d * 1e-3 for d in dims)
    floor = enc.get("floor", "feet")
    if floor not in FLOOR_SHARE:
        raise CheckError(f"{where}: floor must be one of {sorted(FLOOR_SHARE)}")
    area = 2 * (x * z + y * z) + x * y * (1 + FLOOR_SHARE[floor])
    wall = _num(enc, "wall_mm", where, default=2.0) * 1e-3
    k = _num(enc, "k_w_mk", where, default=PLASTIC_K)
    g_walls = area / (1 / H_IN + wall / k + 1 / H_OUT)
    vents = [_num(enc, key, where) for key in ("inlet_mm2", "riser_mm2",
                                                 "outlet_mm2") if key in enc]
    a_vent = (sum(1 / (a * 1e-6) ** 2 for a in vents) ** -0.5
              if vents else 0.0)
    stack = _num(enc, "stack_mm", where, default=dims[2]) * 1e-3
    room = _num(enc, "room_c", where, positive=False, default=DEFAULT_ROOM_C)
    heat = heat_w + _num(enc, "other_w", where, positive=False, default=0.0)

    def g_vent(dt):
        if a_vent <= 0 or dt <= 0:
            return 0.0
        q = CD_VENT * a_vent * math.sqrt(2 * G_ACC * stack * dt /
                                         (room + 273.15))
        return RHO_CP * q

    lo, hi = 0.0, 1000.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if (g_walls + g_vent(mid)) * mid < heat:
            lo = mid
        else:
            hi = mid
    rise = hi if heat > 0 else 0.0
    air = room + rise
    facts = {"box_mm": [checklib.rnd(d) for d in dims],
             "heat_w": checklib.rnd(heat),
             "g_walls_w_k": checklib.rnd(g_walls),
             "g_vents_w_k": checklib.rnd(g_vent(rise)),
             "vent_path_mm2": checklib.rnd(a_vent * 1e6),
             "room_c": room, "air_rise_c": checklib.rnd(rise),
             "air_c": checklib.rnd(air)}
    violations = []
    mat = str(enc.get("material", "")).strip().upper()
    if "tg_c" in enc:
        tg = _num(enc, "tg_c", where)
    elif mat in PLASTIC_TG_C:
        tg = PLASTIC_TG_C[mat]
    else:
        raise CheckError(f"{where}: material must be one of "
                         f"{sorted(PLASTIC_TG_C)}, or give tg_c")
    facts.update(material=mat or None, tg_c=tg,
                 tg_margin_c=checklib.rnd(tg - air))
    sev = ("error" if tg - air < TG_ERROR_K else
           "warning" if tg - air < TG_WARN_K else None)
    if sev:
        violations.append(violation(
            SCRIPT, sev, None, None, None, [],
            f"the box air reaches ~{air:.0f} C ({heat:.1f} W in a "
            f"{dims[0]:.0f} x {dims[1]:.0f} x {dims[2]:.0f} mm box, "
            f"{room:.0f} C room): {tg - air:.0f} K under "
            f"{mat or 'the plastic'}'s Tg ({tg:.0f} C); vent it (inlet low, "
            "outlet high, a clear riser past the board) or print it in a "
            "plastic with a higher Tg", SCRIPT, kind="thermal_enclosure",
            air_c=checklib.rnd(air), tg_c=tg))
    return air, facts, violations


def check_ripple(bg: geom.BoardGeom, entry: dict, air_c):
    """(violations, facts) for one bulk capacitor's ripple and life."""
    ref = entry.get("ref")
    where = f"ripple {ref}"
    if not ref or ref not in bg.footprints:
        raise CheckError(f"{where}: needs a ref on the board")
    if not str(entry.get("source", "")).strip():
        raise CheckError(f"{where}: the rating needs a source")
    irms = _num(entry, "irms_a", where)
    rated = (_num(entry, "rated_irms_a", where) *
             _num(entry, "freq_multiplier", where, default=1.0))
    ratio = irms / rated
    pads = bg.pads_of(ref=ref)
    pos = ((sum(p.center[0] for p in pads) / len(pads),
            sum(p.center[1] for p in pads) / len(pads)) if pads else None)
    vs = []
    facts = {"ref": ref, "irms_a": irms, "rated_irms_a": checklib.rnd(rated),
             "ripple_share": checklib.rnd(ratio)}

    def flag(sev, msg, **kw):
        vs.append(violation(SCRIPT, sev, pos, None, None, [ref], msg, SCRIPT,
                            kind="thermal_ripple", **kw))

    fix = ("add low-ESR ceramic bulk at the switching node or pick a "
           "low-impedance series")
    if ratio > RIPPLE_WARN:
        flag("error" if ratio > 1 + 1e-9 else "warning",
             f"{ref} carries {irms:g} A rms ripple, {ratio:.0%} of its "
             f"{rated:.2f} A rating: {fix}", ripple_share=checklib.rnd(ratio))
    if "irms_max_a" in entry:
        rmax = _num(entry, "irms_max_a", where) / rated
        facts["ripple_share_max"] = checklib.rnd(rmax)
        if rmax > 1 + 1e-9:
            flag("warning", f"{ref} at full power carries "
                 f"{rmax * rated:g} A rms, {rmax:.0%} of its rating: only a "
                 f"power limit keeps that short; {fix}",
                 ripple_share=checklib.rnd(rmax))
    if "life_h" in entry:
        if air_c is None:
            raise CheckError(f"{where}: life needs the air temperature "
                             "(an enclosure or ambient_c)")
        life0 = _num(entry, "life_h", where)
        tr = _num(entry, "rated_c", where)
        tcap = air_c + CAP_CORE_RISE_K * ratio ** 2
        chem = entry.get("chem", "electrolytic")
        if chem not in ("electrolytic", "polymer"):
            raise CheckError(f"{where}: chem is electrolytic or polymer")
        life = life0 * (2 ** ((tr - tcap) / 10) if chem == "electrolytic"
                        else 10 ** ((tr - tcap) / 20))
        facts.update(cap_c=checklib.rnd(tcap), life_h=checklib.rnd(life))
        if "life_min_h" in entry and life < _num(entry, "life_min_h", where):
            flag("warning", f"{ref} lasts ~{life:.0f} h at {tcap:.0f} C "
                 f"({life0:g} h at {tr:g} C), under the "
                 f"{float(entry['life_min_h']):g} h wanted",
                 life_h=checklib.rnd(life))
    return vs, facts


def theta_ja(area_mm2: float, multilayer: bool) -> float:
    t0, tfloor, tau = MODEL_ML if multilayer else MODEL_2L
    a = max(0.0, min(area_mm2, A_SAT_MM2))
    return tfloor + (t0 - tfloor) * math.exp(-a / tau)


def fin_theta(r0_mm: float, area_mm2: float, cu_mm: float,
              h_w_m2k: float = H_SPREAD_W_M2K, sides: int = 2) -> float:
    """Annular copper fin heated at r0, insulated at the radius holding
    area_mm2, cooled by h on `sides` faces: the exact Bessel solution plus
    the heated disc's own faces (K/W). The raw spreading model (module doc)."""
    from scipy.special import i0, i1, k0, k1  # noqa: PLC0415
    k_t = K_CU_W_MK * cu_mm * 1e-3
    m = math.sqrt(sides * h_w_m2k / k_t)
    r0 = r0_mm * 1e-3
    ro = max(r0_mm, math.sqrt(area_mm2 / math.pi)) * 1e-3
    a, b = m * r0, m * ro
    g_fin = (2 * math.pi * k_t * a * (k1(a) * i1(b) - i1(a) * k1(b))
             / (k0(a) * i1(b) + i0(a) * k1(b)))
    return float(1.0 / (g_fin + sides * h_w_m2k * math.pi * r0 ** 2))


def spread_model(fp_area_mm2: float, cu_mm: float, multilayer: bool) -> dict:
    """The calibrated curve extended past A_SAT by the fin (module doc):
    theta(area) for any area, the unbounded-sheet theta, and the area
    within SPREAD_WITHIN of it."""
    tau = (MODEL_ML if multilayer else MODEL_2L)[2]
    r0 = max(1.0, math.sqrt(fp_area_mm2 / math.pi))
    f_sat = fin_theta(r0, A_SAT_MM2, cu_mm)
    th_sat = theta_ja(A_SAT_MM2, multilayer)
    c = ((theta_ja(tau, multilayer) - th_sat)
         / (fin_theta(r0, tau, cu_mm) - f_sat))

    def theta(area_mm2: float) -> float:
        if area_mm2 <= A_SAT_MM2:
            return theta_ja(area_mm2, multilayer)
        return th_sat + c * (fin_theta(r0, area_mm2, cu_mm) - f_sat)

    sheet = theta(math.pi * SHEET_R_MM ** 2)
    lo, hi = A_SAT_MM2, math.pi * SHEET_R_MM ** 2
    if theta(lo) <= sheet * (1 + SPREAD_WITHIN):
        hi = lo
    for _ in range(60):
        if hi - lo < 1.0:
            break
        mid = (lo + hi) / 2
        if theta(mid) > sheet * (1 + SPREAD_WITHIN):
            lo = mid
        else:
            hi = mid
    return {"theta": theta, "sheet_cw": sheet, "sat_mm2": hi}


def via_r_cw(drill_mm: float, board_mm: float = DEFAULT_BOARD_MM) -> float:
    """Barrel conduction of one plated, unfilled via (C/W); inf if no drill."""
    if drill_mm <= 0:
        return math.inf
    r = drill_mm / 2.0
    a_m2 = math.pi * ((r + PLATING_MM) ** 2 - r ** 2) * 1e-6
    return (board_mm * 1e-3) / (K_CU_W_MK * a_m2)


def via_array_r_cw(drills_mm: list[float],
                   board_mm: float = DEFAULT_BOARD_MM) -> float:
    """Parallel vias (the VIA_BENEFIT_CAP largest drills); inf if none."""
    best = sorted((d for d in drills_mm if d > 0), reverse=True)
    g = sum(1.0 / via_r_cw(d, board_mm) for d in best[:VIA_BENEFIT_CAP])
    return 1.0 / g if g > 0 else math.inf


def theta_2l_network(a_top: float, a_bot: float, r_vias: float,
                     min_bot: float = 0.0) -> float:
    """Top pour in parallel with (via array + back pour); see module doc.
    No vias, or a back pour smaller than `min_bot`, leaves the top pour alone."""
    top = theta_ja(a_top, False)
    if math.isinf(r_vias) or a_bot <= 0 or a_bot < min_bot:
        return top
    return 1.0 / (1.0 / top + 1.0 / (r_vias + theta_ja(a_bot, False)))


def _connected_area(copper, seeds, reach) -> float:
    """Area of the copper pieces within reach that touch any seed shape."""
    local = copper.intersection(reach)
    pieces = getattr(local, "geoms", [local])
    return sum(g.area for g in pieces
               if not g.is_empty and any(g.intersects(s) for s in seeds))


def part_region(bg: geom.BoardGeom, ref: str):
    """(centroid, side_layer, footprint_area, pads) for a refdes."""
    pads = bg.pads_of(ref=ref)
    if not pads:
        raise CheckError(f"thermal part {ref!r} has no pads on board")
    cx = sum(p.center[0] for p in pads) / len(pads)
    cy = sum(p.center[1] for p in pads) / len(pads)
    side = "F.Cu" if sum("F.Cu" in p.layers for p in pads) >= \
        sum("B.Cu" in p.layers for p in pads) else "B.Cu"
    hull = geom._union([p.poly for p in pads]).convex_hull
    return (cx, cy), side, hull.area, pads


def check_part(bg: geom.BoardGeom, entry: dict, air_c: float | None = None):
    """(violations, facts) for one dissipating part; air_c is the air it
    sits in (the enclosure's, or ambient_c), None when unknown."""
    ref = entry.get("ref")
    net = entry.get("net")
    if not ref or not net or "power_w" not in entry:
        raise CheckError(f"thermal entry needs ref/net/power_w: {entry}")
    if net not in bg.nets:
        raise CheckError(f"thermal net {net!r} not on board")
    power = float(entry["power_w"])
    dt = float(entry.get("dt_c", DEFAULT_DT_C))
    if "tj_max_c" in entry:
        if air_c is None:
            raise CheckError(f"thermal {ref}: tj_max_c needs the air "
                             "temperature (an enclosure or ambient_c)")
        dt = _num(entry, "tj_max_c", f"thermal {ref}") - air_c
    (cx, cy), side, fp_area, _ = part_region(bg, ref)
    multilayer = len(bg.copper_layers) >= 4

    # heatsink copper = the net's copper WITHIN reach of the part (heat spreads
    # ~1 in radius; a distant pour of the same net elsewhere on the board is not
    # a heatsink for this part). Summed over layers because thermal vias tie the
    # part's pad to inner/back planes; capped where copper stops helping.
    reach = Point(cx, cy).buffer(REACH_MM)
    a_eff = min(A_SAT_MM2, sum(bg.net_copper(net, layer).intersection(reach).area
                               for layer in bg.copper_layers))
    theta = theta_ja(a_eff, multilayer)

    # past A_SAT copper still helps (module doc): the spreading model's area
    # and theta, advice only - pass/fail keeps the clamped screen above
    cu = bg.stackup.copper_thickness
    cu_mm = (sum(cu.get(lyr) or DEFAULT_CU_MM for lyr in bg.copper_layers)
             if multilayer else cu.get(side) or DEFAULT_CU_MM)
    spread = spread_model(fp_area, cu_mm, multilayer)
    r_sat = math.sqrt(spread["sat_mm2"] / math.pi)
    # the footprint the heat spreads over (the layers' copper thickness is
    # already in cu_mm, so the layers are not summed again here)
    a_far = geom._union([bg.net_copper(net, layer) for layer in
                         bg.copper_layers]).intersection(
        Point(cx, cy).buffer(r_sat)).area
    spread_facts = {"copper_sat_mm2": checklib.rnd(spread["sat_mm2"]),
                    "area_far_mm2": checklib.rnd(a_far),
                    "theta_spread_cw": checklib.rnd(spread["theta"](a_far)),
                    "theta_sheet_cw": checklib.rnd(spread["sheet_cw"])}

    # thermal vias of the net under the part (needed once copper saturates):
    # board vias plus the plated drills of the net's own pads there (a
    # footprint's thermal-via pads, a THT tab) - both tie the layers together
    r_win = max(2.0, math.sqrt(fp_area / math.pi) + 1.5)
    region = Point(cx, cy).buffer(r_win)
    vias = [v for v in bg.vias_of(net) if region.contains(Point(v.at))]
    drilled = [p for p in bg.pads_of(net=net) if p.drill and len(p.layers) > 1
               and region.contains(Point(p.center))]
    n_vias = len(vias) + len(drilled)

    network = {}
    if not multilayer and len(bg.copper_layers) == 2:
        bot = next(lyr for lyr in bg.copper_layers if lyr != side)
        board_mm = bg.stackup.total_thickness or DEFAULT_BOARD_MM
        r_vias = via_array_r_cw(
            [v.drill for v in vias] + [min(p.drill) for p in drilled], board_mm)
        seeds = [v.poly for v in vias] + [p.poly for p in drilled]
        a_top = min(A_SAT_MM2, _connected_area(
            bg.net_copper(net, side),
            [p.poly for p in bg.pads_of(net=net, ref=ref)], reach))
        a_bot = min(A_SAT_MM2, _connected_area(
            bg.net_copper(net, bot), seeds, reach)) if seeds else 0.0
        theta_net = theta_2l_network(a_top, a_bot, r_vias, min_bot=fp_area)
        best_drill = max([BEST_VIA_DRILL_MM] + [v.drill for v in vias]
                         + [min(p.drill) for p in drilled])
        r_best = via_array_r_cw([best_drill] * VIA_BENEFIT_CAP, board_mm)
        theta_best = theta_2l_network(A_SAT_MM2, A_SAT_MM2, r_best)
        t_big = spread["theta"](spread["sat_mm2"])
        theta_best_big = 1.0 / (1.0 / t_big + 1.0 / (r_best + t_big))
        network = {"area_top_mm2": checklib.rnd(a_top),
                   "area_bottom_mm2": checklib.rnd(a_bot),
                   "via_array_r_cw": (None if math.isinf(r_vias)
                                      else checklib.rnd(r_vias)),
                   "back_pour_reached": bool(seeds) and a_bot >= fp_area,
                   "theta_area_cw": checklib.rnd(theta),
                   "theta_network_cw": checklib.rnd(theta_net),
                   "theta_best_2l_cw": checklib.rnd(theta_best),
                   "theta_best_2l_spread_cw": checklib.rnd(theta_best_big)}
        theta = min(theta, theta_net)

    # a datasheet self-heating rating replaces the copper model; declared
    # forced air scales whichever theta is used (module doc, SCBA017D)
    violations: list[dict] = []
    lfm = entry_airflow_lfm(entry)
    af = airflow_factor(lfm)
    rated = "rating" in entry
    slug = slug_top_info(bg, ref, entry)
    has_hs = "heatsink" in entry
    if rated and (slug or has_hs):
        raise CheckError(f"thermal {ref}: a rated part takes no slug or "
                         "heatsink")
    extra = {}
    if slug:
        # the leads are the board path: no via or 2-layer network credit
        theta = theta_ja(a_eff, multilayer)
        extra.update(slug="top", package=slug["package"],
                     slug_source=slug["source"])

    def constraint_error(msg):
        violations.append(violation(
            SCRIPT, "error", (cx, cy), side, net, [ref], msg, SCRIPT,
            kind="thermal_constraint"))

    if rated:
        extra["theta_model_cw"] = checklib.rnd(theta)
        theta = rated_theta_cw(entry)
        extra["theta_rated_cw"] = checklib.rnd(theta)
    datasheet = "theta_ja_c_w" in entry
    if datasheet:
        extra["theta_model_cw"] = checklib.rnd(theta)
        theta = datasheet_theta_cw(entry)
        why, theta = theta_sanity(entry, slug, theta, has_hs)
        if why:
            constraint_error(why)
        extra["theta_datasheet_cw"] = checklib.rnd(theta)
    elif slug and slug.get("theta_ja_c_w"):
        extra["theta_model_cw"] = checklib.rnd(theta)
        theta = slug["theta_ja_c_w"]
        extra["theta_datasheet_cw"] = theta
        datasheet = True
    elif slug:
        constraint_error(
            f"{ref} is a pad-up package ({slug['package']}): the copper "
            "model assumes a pad soldered down, so give its datasheet "
            "no-heatsink theta_ja_c_w (with theta_source)")
    theta_board = theta
    r_hs = None
    if has_hs:
        r_hs, hs_facts, hs_why = heatsink_path(bg, entry, slug)
        for why in hs_why:
            violations.append(violation(
                SCRIPT, "error", (cx, cy), side, net, [ref], f"{ref}: {why}",
                SCRIPT, kind="thermal_heatsink"))
        theta = 1.0 / (1.0 / theta_board + 1.0 / r_hs)
        extra.update(hs_facts, theta_board_cw=checklib.rnd(theta_board))
    if lfm > 0:
        extra.update(theta_still_air_cw=checklib.rnd(theta),
                     airflow_lfm=checklib.rnd(lfm),
                     airflow_factor=checklib.rnd(af))
        theta *= af
    rise = power * theta

    # copper alone bottoms out at theta_ja(A_SAT) (the clamp), NOT the model's
    # asymptotic floor - if the target is below that, only vias/planes reach it.
    # A rated part's heat path is its own body, so no via array is asked of it.
    floor_cw = theta_ja(A_SAT_MM2, multilayer) * af
    need_vias = ((dt / power) < floor_cw if power > 0 and not rated
                 and not slug and not has_hs else False)
    # an entry's min_vias is the datasheet's floor, held on every part; the
    # computed array is asked only when the copper can't meet the budget
    explicit = "min_vias" in entry
    if explicit:
        v = _num(entry, "min_vias", f"thermal {ref}", positive=False)
        if not (math.isfinite(v) and v >= 0):
            raise CheckError(f"thermal {ref}: min_vias must be a count >= 0: "
                             f"{v:g}")
        min_vias = math.ceil(v)
    else:
        min_vias = min(VIA_BENEFIT_CAP,
                       max(4, math.ceil(fp_area / VIA_PITCH_MM ** 2)))

    copper_model = not (rated or slug or has_hs or datasheet)
    # 4+ layers: a_eff sums the net's inner planes, which the part reaches
    # only through vias. Short of VIA_CREDIT_MIN (or a lower explicit floor,
    # but at least one) the screen also reads the part on its own side's
    # copper, and the truth lies between the two (module doc).
    bounded = multilayer and copper_model
    credit_vias = (max(1, min(min_vias, VIA_CREDIT_MIN)) if explicit
                   else VIA_CREDIT_MIN)
    credited = not bounded or n_vias >= credit_vias
    not_judged: list[str] = []
    side_facts = {}
    if bounded:
        a_side = min(A_SAT_MM2, _connected_area(
            bg.net_copper(net, side),
            [p.poly for p in bg.pads_of(net=net, ref=ref)], reach))
        theta_side = theta_ja(a_side, True) * af
        rise_side = power * theta_side
        # a small part's own copper can be its pads alone (a fraction of a mm2)
        side_mm2 = f"{a_side:.0f}" if a_side >= 10 else f"{a_side:.1f}"
        side_facts = {"planes_credited": credited,
                      "planes_credit_vias": credit_vias,
                      "area_side_mm2": checklib.rnd(a_side),
                      "theta_side_cw": checklib.rnd(theta_side),
                      "rise_side_c": checklib.rnd(rise_side),
                      "power_limit_side_w": checklib.rnd(
                          dt / theta_side if theta_side > 0 else math.inf)}
    side_note = ("" if credited else
                 f"; only {n_vias} via(s) near it reach the {net} planes "
                 f"(the screen credits them from {credit_vias}), and on its "
                 f"own {side} copper ({side_mm2} mm2) it rises "
                 f"~{rise_side:.0f} C")

    def grow_hint(theta_big: float, what: str) -> str:
        """', or grow ...' when copper past the screen's 1 in^2 would meet
        the budget by the spreading model (module doc); else ''."""
        # 0.98: the buffered circle is a polygon, ~0.6 % short of pi r^2
        if (not copper_model or a_far >= 0.98 * spread["sat_mm2"]
                or power * theta_big * af > dt + 1e-6):
            return ""
        return (f", or grow {what} to ~{spread['sat_mm2']:.0f} mm2 within "
                f"{r_sat:.0f} mm of {ref} (a spreading model gives "
                f"~{theta_big * af:.0f} C/W there, ~{power * theta_big * af:.0f}"
                f" C; the screen credits copper only to {A_SAT_MM2:.0f} mm2)")

    if rise > dt + 1e-6:
        saturated = a_eff >= A_SAT_MM2 - 1e-6
        remedy = ("add thermal vias to an inner/back plane" + grow_hint(
            spread["theta"](spread["sat_mm2"]), f"the {net} pour")
            if saturated or need_vias else f"grow the {net} pour")
        if bounded and (saturated or need_vias):
            # 4+ layers: the reading already credits the net's inner planes
            # and doesn't count vias past that (module doc), so "add vias"
            # is a remedy a re-run can't confirm (#1181).
            remedy = (f"more vias won't change this reading (the screen "
                      f"already credits the {net} inner planes and has no "
                      f"via lever past that): cut the loss, give {ref} its "
                      "datasheet theta_JA (theta_ja_c_w with theta_source), "
                      "or add a heatsink or forced air") + grow_hint(
                spread["theta"](spread["sat_mm2"]), f"the {net} pour")
        where = f"into {a_eff:.0f} mm2 of {net} copper"
        if rated:
            r = entry["rating"]
            where = (f"by its own rating ({float(r['rise_c']):.0f} C at "
                     f"{float(r['current_a']):g} A)")
            remedy = ("cut the loss, pick a part rated for more current, "
                      "or add forced air")
        elif has_hs:
            where = (f"through its heatsink ({r_hs:.0f} C/W) in parallel "
                     f"with the board ({theta_board:.0f} C/W)")
            remedy = ("fit a heatsink with a lower theta_sa, a thinner, "
                      "better interface (a clamped pad, not tape), or cut "
                      "the loss")
        elif slug:
            where = (f"on the board alone ({theta_board:.0f} C/W, no "
                     "heatsink)")
            remedy = (f"its slug is on top ({slug['package']}), so vias "
                      "and copper can't cool it: fit a heatsink to the "
                      "slug and declare it under heatsink")
        elif datasheet:
            where = (f"on its datasheet theta_JA "
                     f"({extra['theta_datasheet_cw']:g} C/W, JEDEC board)")
            remedy = "cut the loss, add a heatsink, or add forced air"
        elif network:
            where = (f"through {network['area_top_mm2']:.0f} mm2 top + "
                     f"{network['area_bottom_mm2']:.0f} mm2 back {net} copper "
                     f"and {n_vias} thermal via(s)")
            best = network["theta_best_2l_cw"] * af
            if power * best > dt + 1e-6:
                remedy = (f"even the best 2-layer case ({VIA_BENEFIT_CAP} vias "
                          f"into full pours, ~{best:.0f} C/W) gives "
                          f"~{power * best:.0f} C: cut the dissipation, add a "
                          "heatsink, or move to 4 layers") + grow_hint(
                    theta_best_big, f"both {net} pours (with the vias)")
            elif not network["back_pour_reached"] or n_vias < VIA_BENEFIT_CAP:
                remedy = f"add thermal vias into a back-side {net} pour"
            else:
                remedy = f"grow the top and back {net} pours"
        if lfm > 0:
            where += f" at {lfm:.0f} LFM"
        violations.append(violation(
            SCRIPT, "error", (cx, cy), side, net, [ref],
            f"{ref} dissipates {power:.2f} W {where}: "
            f"~{rise:.0f} C rise (theta_JA ~{theta:.0f} C/W) "
            f"> {dt:.0f} C allowed; {remedy}{side_note}", SCRIPT,
            kind="thermal_area",
            power_w=power, area_mm2=checklib.rnd(a_eff),
            theta_ja=checklib.rnd(theta), rise_c=checklib.rnd(rise),
            dt_allowed_c=dt,
            **({} if credited else {"rise_side_c": checklib.rnd(rise_side)})))
    elif not credited and rise_side > dt + 1e-6:
        not_judged.append(
            f"{ref}: ~{rise:.0f} C rise with the {net} inner planes "
            f"credited, ~{rise_side:.0f} C on its own {side} copper "
            f"({side_mm2} mm2), against {dt:.0f} C allowed; it has "
            f"{n_vias} thermal via(s) near it, under the {credit_vias} the "
            "screen needs to credit the planes, so it can't tell which "
            f"holds (add >= {credit_vias} vias to the {net} planes to settle "
            "it)")
    if explicit and n_vias < min_vias:
        violations.append(violation(
            SCRIPT, "error", (cx, cy), side, net, [ref],
            f"{ref} has {n_vias} thermal via(s) to {net} within "
            f"{r_win:.1f} mm of its centre, under the {min_vias} its "
            "min_vias asks: add the missing vias (min_vias is the "
            "datasheet's floor; lower it only if the datasheet allows "
            "fewer)", SCRIPT, kind="thermal_vias",
            vias=n_vias, required=min_vias, power_w=power, explicit=True,
            window_mm=checklib.rnd(r_win)))
    elif not explicit and need_vias and n_vias < min_vias:
        violations.append(violation(
            SCRIPT, "warning", (cx, cy), side, net, [ref],
            f"{ref} ({power:.2f} W) needs a thermal-via array to {net} "
            f"(the screen credits copper only to {A_SAT_MM2:.0f} mm2, "
            f"~{floor_cw:.0f} C/W); found {n_vias} "
            f"via(s), want >= {min_vias}", SCRIPT, kind="thermal_vias",
            vias=n_vias, required=min_vias, power_w=power))

    power_limit = dt / theta if theta > 0 else math.inf
    if "power_max_w" in entry:
        pmax = _num(entry, "power_max_w", f"thermal {ref}")
        extra["rise_max_c"] = checklib.rnd(pmax * theta)
        if not credited:
            extra["rise_max_side_c"] = checklib.rnd(pmax * theta_side)
        gov = str(entry.get("governor", "")).strip()
        if gov:
            extra["governor"] = gov
        elif pmax * theta > dt + 1e-6:
            side_limit = ("" if credited else
                          f"; ~{dt / theta_side:.1f} W on its own {side} "
                          f"copper, with only {n_vias} via(s) to the planes")
            violations.append(violation(
                SCRIPT, "warning", (cx, cy), side, net, [ref],
                f"{ref} at its continuous maximum ({pmax:g} W) rises "
                f"~{pmax * theta:.0f} C, over the {dt:.0f} C allowed: it "
                f"needs a limit in firmware holding it under "
                f"~{power_limit:.1f} W{side_limit} (name it in governor)",
                SCRIPT, kind="thermal_governor", power_max_w=pmax,
                power_limit_w=checklib.rnd(power_limit)))
        elif not credited and pmax * theta_side > dt + 1e-6:
            not_judged.append(
                f"{ref}: at its continuous maximum ({pmax:g} W) it rises "
                f"~{pmax * theta:.0f} C with the {net} planes credited, "
                f"~{pmax * theta_side:.0f} C on its own {side} copper, "
                f"against {dt:.0f} C allowed; with {n_vias} via(s) near it "
                "the screen can't tell whether it needs a limit in firmware "
                f"(add >= {credit_vias} vias to the planes, or name one in "
                "governor)")
    if air_c is not None:
        extra.update(air_c=checklib.rnd(air_c), tj_c=checklib.rnd(air_c + rise))
    facts = {"ref": ref, "net": net, "power_w": power,
             "power_limit_w": checklib.rnd(power_limit),
             "area_mm2": checklib.rnd(a_eff), "theta_ja": checklib.rnd(theta),
             "rise_c": checklib.rnd(rise), "dt_allowed_c": dt,
             "vias_near_part": n_vias, "multilayer": multilayer, **network,
             **(spread_facts if copper_model else {}), **side_facts,
             **({"min_vias": min_vias} if explicit else {}), **extra}
    if not_judged:
        facts["not_judged"] = not_judged
    return violations, facts


def thermal_rows(path: Path = THERMAL_PARTS_YAML) -> list[dict]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["parts"]
    return [dict(r, rx=re.compile(r["match"], re.I)) for r in rows]


def thermal_row(fp: dict, rows: list[dict]) -> dict | None:
    """The thermal_parts.yaml row a footprint matches on Value / MPN."""
    props = fp.get("props") or {}
    texts = [fp.get("value") or ""] + [props.get(k) or "" for k in MPN_PROPS]
    return next((r for r in rows
                 if any(t and r["rx"].search(str(t).strip()) for t in texts)),
                None)


def heatsink_net(bg: geom.BoardGeom, ref: str) -> str | None:
    """The net of the part's biggest pad (its exposed pad), if connected."""
    pads = bg.pads_of(ref=ref)
    if not pads:
        return None
    net = max(pads, key=lambda p: p.size[0] * p.size[1]).net
    return net if net and not net.startswith("unconnected-") else None


def loss_w(row: dict, peak_a: float) -> float | None:
    """The row's loss model at a peak phase current (yaml header)."""
    if row.get("loss") == "three_phase":
        return 3 * (peak_a ** 2 / 2) * (float(row["r_hs_ls_ohm"]) / 2)
    return None


def warn_current_a(row: dict, theta: float, air_c: float) -> float | None:
    """Peak phase current at which the die reaches tj_warn_c through theta."""
    if "tj_warn_c" not in row or loss_w(row, 1.0) is None:
        return None
    budget = (float(row["tj_warn_c"]) - air_c) / theta
    return math.sqrt(budget / loss_w(row, 1.0)) if budget > 0 else 0.0


def check_inferred(bg: geom.BoardGeom, ref: str, row: dict,
                   air_c: float | None):
    """(violations, facts) for a known power IC no thermal entry names."""
    fp = bg.footprints[ref]
    part = fp.get("value") or ref
    net = heatsink_net(bg, ref)
    facts = {"ref": ref, "part": part, "inferred": True,
             "theta_ja_datasheet_cw": float(row["theta_ja_c_w"]),
             "source": row["source"]}
    if net is None:
        return [], dict(facts, skipped="its biggest pad has no net")
    _, base = check_part(bg, {"ref": ref, "net": net, "power_w": 1.0,
                              "dt_c": 1e9})
    theta = min(base["theta_ja"], base.get("theta_spread_cw", base["theta_ja"]))
    ds = float(row["theta_ja_c_w"])
    ratio = theta / ds
    air = INFERRED_AIR_C if air_c is None else air_c
    facts.update(net=net, theta_board_cw=checklib.rnd(theta),
                 theta_ratio=checklib.rnd(ratio), air_c=air)
    here, there = warn_current_a(row, theta, air), warn_current_a(row, ds, air)
    if here is not None:
        facts.update(warn_peak_a=checklib.rnd(here),
                     warn_peak_a_datasheet=checklib.rnd(there))
    if ratio <= THETA_RATIO_MAX:
        return [], facts
    (cx, cy), side, _, _ = part_region(bg, ref)
    msg = (f"{ref} ({part}) sheds heat through ~{theta:.0f} C/W of {net} "
           f"copper, {ratio:.1f}x the {ds:g} C/W of its datasheet's JEDEC "
           f"board")
    if here is not None:
        rated = (f", rated {float(row['peak_a']):g} A peak"
                 if "peak_a" in row else "")
        msg += (f": it reaches its thermal warning ({float(row['tj_warn_c']):g}"
                f" C) at ~{here:.1f} A peak per phase from {air:g} C air, "
                f"where the datasheet's board allows ~{there:.1f} A{rated}")
    msg += (f"; add {net} copper and thermal vias under it, or more layers - "
            f"inferred from reference/thermal_parts.yaml ({row['source'].split(':')[0]}); "
            f"give {ref} a thermal entry in constraints.json with this board's "
            "load to judge it instead")
    v = violation(SCRIPT, "warning", (cx, cy), side, net, [ref], msg, SCRIPT,
                  kind="thermal_area", inferred=True,
                  theta_board_cw=checklib.rnd(theta),
                  theta_ja_datasheet_cw=ds, theta_ratio=checklib.rnd(ratio))
    return [v], facts


def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Dissipation vs copper heatsink area (theta_JA screen).")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--constraints",
                    help="constraints.json with a thermal list; without it "
                         "only the known power ICs are screened")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    cons = (checklib.load_json(args.constraints, "constraints")
            if args.constraints else {})
    # unfilled zones: judged on a refilled scratch copy, or (kicad-cli can't
    # refill) as committed with findings on their nets cut to warnings
    bg, fill = geom.fresh_board(Path(args.pcb))

    violations: list[dict] = []
    checked: list[dict] = []
    entries = cons.get("thermal", [])
    air_c = cons.get("ambient_c")
    extra = {}
    if "enclosure" in cons:
        heat = sum(float(e.get("power_w", 0) or 0) for e in entries)
        air_c, extra["enclosure"], vs = enclosure_air(
            cons["enclosure"], heat,
            Path(args.constraints or args.pcb).resolve().parent)
        violations.extend(vs)
    elif air_c is not None:
        air_c = float(air_c)
    not_judged: list[str] = []
    for entry in entries:
        vs, facts = check_part(bg, entry, air_c)
        violations.extend(vs)
        not_judged.extend(facts.pop("not_judged", []))
        checked.append(facts)

    # a known power IC nobody gave a thermal entry: its datasheet's board
    listed = {e.get("ref") for e in entries}
    rows = thermal_rows()
    for ref in sorted(bg.footprints):
        row = None if ref in listed else thermal_row(bg.footprints[ref], rows)
        if row is not None and bg.pads_of(ref=ref):
            vs, facts = check_inferred(bg, ref, row, air_c)
            violations.extend(vs)
            checked.append(facts)

    # a pad-up part nobody gave a thermal entry: nothing says how its slug
    # is cooled
    for ref in sorted(bg.footprints):
        if ref in listed:
            continue
        slug = slug_top_info(bg, ref)
        if slug and bg.pads_of(ref=ref):
            pads = bg.pads_of(ref=ref)
            pos = (sum(p.center[0] for p in pads) / len(pads),
                   sum(p.center[1] for p in pads) / len(pads))
            violations.append(violation(
                SCRIPT, "warning", pos, None, None, [ref],
                f"{ref} is a pad-up package ({slug['part']}, "
                f"{slug['package']}): its slug faces up, for a heatsink; give "
                "it a thermal entry with its loss and its heatsink", SCRIPT,
                kind="thermal_unlisted"))

    ripple = []
    for entry in cons.get("ripple", []):
        vs, facts = check_ripple(bg, entry, air_c)
        violations.extend(vs)
        ripple.append(facts)
    if ripple:
        extra["ripple"] = ripple

    extra_fill = checklib.zone_fill_facts(violations, fill)
    payload = checklib.report(
        SCRIPT, args.pcb, violations, checked=checked,
        nothing_to_judge=None if checked or ripple else
        "no thermal or ripple entries in constraints, and no power IC "
        "reference/thermal_parts.yaml knows",
        not_judged=not_judged + extra_fill.pop("not_judged", []), **extra,
        **extra_fill)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
