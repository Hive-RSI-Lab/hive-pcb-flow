#!/usr/bin/env python
"""cpl_verify.py - does each machine-placed part land with pin 1 on pad 1?

JLC places a part the way its LCSC footprint model is drawn in EasyEDA, turned
by the CPL rotation and put at the CPL position. A package table cannot know
that model (the '^SOT-23,180' row turned the owner's SOT-23-5/6 parts 180 deg),
so this check reads each `smt_placed` part's own model (lib/easyeda.py, cached
per LCSC number) and fits it onto the board footprint:

  rotation  The model's pads are matched to the board footprint's pads BY
            NUMBER. The correction c is the turn (a multiple of 90 deg, else
            45 deg) that puts every shared pad number on its board pad, after
            centring both pad sets (each model pad nearer its own board pad
            than any other). The CPL must say (board rotation + c) mod
            360 on the top and (180 - board rotation + c) mod 360 on the
            bottom (see "bottom side"); anything else is `wrong_rotation`,
            with the rotation it needs. A two-pad part that is not polarised
            or keyed (a JST/Molex housing, a battery: is_keyed) may also sit
            at c + 180, and so may an unkeyed single-row pin header/socket.
            A switch whose pads share numbers (TS-1187A: 1,1,2,2 on the
            board, 1-4 in its model) is fitted by pad geometry.
  polarity  Diodes, LEDs and polarised caps are also matched BY ROLE: the
            model's cathode / + pin (its symbol pin names) must land on the
            board's cathode / + pad (the pad's pinfunction, else the KiCad
            library convention: D_/LED_ pad 1 = K, CP_ pad 1 = +, else - for
            a footprint pulled from the same LCSC part - the model's own
            numbering, else a two-pad LED* footprint on a D/LED refdes, pad
            1 = K as KiCad's Device:LED numbers it). Where roles are named on
            both sides they decide the rotation, even when the numbers fit
            another way (C2286's pad 2 is its K, so pin 1 goes on pad 2): a
            CPL at that number fit puts K on A and is `wrong_polarity`. With no K/A or +/- names on one side (LCSC
            symbols for polarised caps say "1"/"2"), polarity is checked by
            pad number, and a 180-degree turn is never accepted for it.
  no data   No LCSC number, no cached or fetchable model, fewer than two
            shared pad numbers or no turn that fits is a FAILURE, never a
            pass - `no_model` or `no_fit`. One exception (#1024): a part of
            three or more pads whose board pad numbers are the model's in
            another order, and whose pads land by geometry, is
            `pin_numbers_differ`, a warning: JLC places by geometry, so the
            CPL is judged at the geometric fit, and the pin map is named for
            a person to check (oasis v0.3 Q1/Q3, a "pinswapped" SOT-23). A
            CPL off every geometric fit is still `wrong_rotation`. A model fetch that errored (network, HTTP status) is `fetch_failed`,
            never `no_model`: it says nothing about the part or its pin 1.
            One exception: a two-pad part that is not polarised, keyed or a
            switch is checked against the shape every LCSC two-pad model has
            (two_pad_stand_in: its pads along x, centred on the origin; 508
            of 508 cached models, 2026-11-13), so a CPL turned 90 deg or off
            its pads still fails and one either way round passes, with
            `basis: two-pad axis`. oasis v0.2's L3 (C844983, an IHLP-2020
            EasyEDA has no model for) was a false failure for want of this.
            Like the ball grid below, it waits while a fetch could still
            answer.
            A 403/429 is EasyEDA's rate limit: later parts are not fetched
            (also `fetch_failed`) and the report's `rate_limited` says why.
  footprint A ball grid with no model (no LCSC number, or EasyEDA has none) is
            checked against the package's own A1 corner, read from its ball
            names: CPL = board rotation + the turn from a part with A1 upper
            left, columns running right (IPC-7351 Level A). `basis: footprint`.
            A match is `footprint_match`, a warning and never a pass: JLC's
            zero is the part's tape orientation and it publishes no BGA rule
            (reference/jlc_bga_cpl.md). A mismatch is `wrong_rotation`. Names
            that give no A1 (a mirrored land pattern, a skewed grid) stay
            `no_model`. A fetch that failed, or an LCSC number not yet fetched
            under --offline, gets no footprint verdict: the model can answer.
            Offline, EasyEDA's earlier "no model" answer is read from the
            `<LCSC>.none` kept beside the cached models, so a test or score
            run judges such a part as an online run does; a part never asked
            about is `fetch_failed` with the offline reason, never no_model.
            Position isn't checked on this basis.
  offset    The model's pad centre vs the board's, both placed, is reported
            (`offset_mm`); over OFFSET_WARN_MM it is a warning only. The
            limit shrinks with the pad pitch (`offset_limit_mm`: 0.4 x the
            closest pad centres, never over OFFSET_WARN_MM): a part whose
            limit is below that (a 0.5 mm BGA: 0.2 mm) is `wrong_offset`, a
            failure, because the offset alone leaves its balls half off.

Bottom side. The board is viewed from below, turned about its y axis (x -> -x,
as a page is turned), with the part read from its own side. Writing R(a) for a
turn and M for KiCad's flip (local y -> -y, which the board file already holds
in a flipped footprint's pads), the pads sit at R(r) M L, with L the unflipped
land pattern and the model fitting it as R(c) model = L. The view from below is
V = diag(-1, 1), and V R(r) M R(c) = R(180 - r + c). So the part is fitted
against the pads un-flipped, and the CPL rotation is (180 - r + c) mod 360. A
flip about the x axis instead would give (-r + c): that 180 degrees is exactly
what the choice of view decides, and geometry alone can't choose it.

The choice comes from two community exporters, not from JLC: Bouni
kicad-jlcpcb-tools (fabrication.py, "bottom angles need to be mirrored on
Y-axis": (180 - rotation) % 360, correction added after) and bennymeg
Fabrication-Toolkit (process.py, "JLC expect 'Rotation' to be 'as viewed from
above component'": 180.0 - rotation, offsets added after). JLC's order preview
is what settles it, so a passing bottom part still gets `bottom_preview` in the
report and a dfm warning (cpl_bottom_preview). The two readings differ by
exactly 180 degrees, so a part that is right either way round (an unpolarised
two-pad part: oasis v0.2's C4) is right under both and gets no warning.
reference/jlc_bottom_cpl.md has the record; add what a preview or a built
board shows there.

The pin-1 silk mark: JLC places by the silkscreen when it disagrees with the
CPL, and with no mark it can't read the polarity and confirms by email. So for
ball grids and U/IC parts of three or more pads, `silk_mark()` finds the dots,
small shapes and corner chamfers on the part's own side, and takes each one's
nearest corner of the pad field. One off pin 1's corner is `silk_mark_wrong` in
the report and fails the run; none at all is `silk_mark_missing`, a warning,
since the email costs a delay and not a wrong part
(reference/jlc_bga_cpl.md). Q parts (SOT-23 and the like) are held to the same
pin-1 rule, but with no mark they pass: their 2+1 land shows the way round.
Two-pad diodes, LEDs and polarised caps go through `polarity_mark()`: a "+"
must sit by the board's + pad, else (but for electrolytics, whose hatching and
chamfers lean either way) the silk must lean toward the K or + pad; leaning
the other way is `silk_mark_wrong`, centred is `silk_mark_missing` (#800).
Battery holders and switches are keyed housings, left alone. Where a part's own
silk has no mark, silk drawn on the board itself (gr_circle, gr_line, gr_poly,
gr_rect, gr_arc and a "+" gr_text on its side's silk layer) is read too when
the whole drawing sits within BOARD_MARK_MARGIN_MM of the part's pads; its row
says `silk_source: board`. A drawing that close to two parts counts for
neither, and an unmarked part's row says so in `silk_note` (#1380).

`derive_rotation()` is what bom_cpl.py uses to WRITE the CPL; the package table
is only its fallback when this check has no model. `merge_visual()` puts the
fab step's image verdict (cpl_render.py + the playbook's vision agent, file
`cpl_visual.json`) beside the script verdict: a part the two disagree on, or
one the visual pass skipped, fails.

CLI:
  cpl_verify.py --pcb board.kicad_pcb --cpl fab/CPL.csv
                [--parts parts.json] [--cache-dir DIR] [--offline]
                [--visual fab/cpl_visual.json] [--out report.json]
--cpl may also be KiCad's own placement file (Ref,Val,Package,PosX,PosY,Rot,
Side): its rotations are the board's, read with no correction
(read_placement), and the report says so in `cpl_note`.
LCSC numbers come from the board's footprint fields, overridden by parts.json.
The cache is $HWDE_EASYEDA_CACHE, else <parts.json dir>/easyeda (made on the
first fetch - commit it), else ~/.cache/hwde/easyeda (env.easyeda_cache), which
is also read when the board's has no copy; --offline (or HWDE_EASYEDA_OFFLINE=1)
never fetches.
--vendor (default: the workspace's chosen vendor, else JLC) names the
assembler, whose profile's assembly.rotation says what a CPL rotation means
(#1068, convention()): `jlc` is all of the above; `kicad` (PCBWay, Aisler,
Seeed, Eurocircuits) is KiCad's own rotation as exported, so each row's
position, side and rotation must be its board footprint's (verify_vendor,
`basis: kicad`, no LCSC model; an unpolarised two-pad part either way round),
and a profile's `zero_orientation` (Eurocircuits' IEC 61188-7 zero) leaves
the parts of three or more pads `not_judged`; a fab with no assembly table
(OSH Park, Advanced Circuits) judges no row: status `not_judged`.
Exit 0 every part verified / 1 a part is wrong or unverified / 2 error /
3 not judged (no convention for the vendor).
In a board workspace each part's `footprint` fact goes into its ledger.json
(record_footprints, lib/ledgerlib.py) - from dfm's placement leg too.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import sexpdata  # noqa: E402

import bgalib  # noqa: E402
import easyeda  # noqa: E402

OFFSET_WARN_MM = 0.5
OFFSET_PITCH_FRAC = 0.4   # an offset past this share of the pad pitch is a failure
ROT_TOL_DEG = 1.0
FAIL_VERDICTS = ("wrong_rotation", "wrong_polarity", "no_model", "no_fit",
                 "fetch_failed", "wrong_offset", "wrong_side",
                 "wrong_position")
# Not in FAIL_VERDICTS, and not "ok": the CPL meets a convention, not a model.
FOOTPRINT_MATCH = "footprint_match"
# Not a failure either: the pads land, but by geometry, not by number (#1024).
PIN_MAP = "pin_numbers_differ"
PIN_MAP_NOTE = (
    "the LCSC model's pads land on the board's only with their numbers "
    "permuted: JLC places by pad geometry, so the CPL rotation given is the "
    "geometric fit, but check the symbol-to-footprint pin map (which pin is "
    "on which pad) against the part's datasheet before ordering")
BOTTOM_PREVIEW_NOTE = (
    "bottom-side rotations follow (180 - kicad + correction), from two "
    "community exporters and the geometry, not from a JLC statement: check "
    "these parts' pin 1 in JLC's order preview before ordering")
FOOTPRINT_BASIS_NOTE = (
    "no LCSC model, so the CPL rotation was checked only against the package's "
    "IPC-7351 A1 corner, and position not at all. JLC's zero is the part's tape "
    "orientation and it publishes no BGA rule, so this is not a pass: give the "
    "part an LCSC number, or confirm A1 in JLC's order preview against the "
    "part's tape spec before ordering (reference/jlc_bga_cpl.md)")
TWO_PAD_BASIS = "two-pad axis (no LCSC model)"
NO_SILK_MARK_NOTE = (
    "no pin-1 dot or chamfer (or, on a diode, LED or polarised cap, no "
    "cathode bar or \"+\" off centre) found on the part's own silkscreen. JLC can't read "
    "the polarity with no mark and confirms by email, which holds the order: "
    "add a mark at pin 1, or answer the email (reference/jlc_bga_cpl.md)")
# What counts as a pin-1 mark: a dot, a small drawn shape, or a line cutting
# a corner. Sizes are from hw-lab's real marks: dots to r 0.2 mm, triangles and
# chamfers to 0.8 mm. MSOP side notches (r 0.55, open arcs) and outlines are out.
MARK_MAX_R_MM = 0.75
MARK_MAX_SPAN_MM = 2.0
CHAMFER_MAX_MM = 3.0
CORNER_TIE_MM = 0.1
ARC_CLOSED_MM = 0.01  # EasyEDA dots' ends miss by rounding: SOT-25 U402, 0.002 mm
_MARK_REF = re.compile(r"^(U|IC)\d")
# A transistor's 2+1 land shows its pin 1 on its own, so only a mark at the
# wrong pin counts against it: no mark is no email (#800).
_TRANSISTOR_REF = re.compile(r"^Q\d")
# A two-pad polar part's mark: how far the silk sits toward its K or + pad, as
# a share of half the pad spacing. Every marked diode, LED and tantalum in the
# training corpus (2026-11-15) sits at 0.22 or more; centred outlines at 0.06
# or less. An electrolytic's silk is judged by its "+" alone: KiCad hatches
# CP_Radial's minus side and chamfers CP_Elec's plus side, so where the silk
# sits says nothing.
POLAR_MARK_SHARE = 0.15
# cut outline corners count only out by the pad ends, past the body's middle
CHAMFER_SHARE = 0.6
PLUS_MIN_MM, PLUS_MAX_MM = 0.3, 2.5
# board-level silk counts for a part when all of it lies within this of the
# part's pad copper (#1380): a pin-1 dot sits by its pad, a mounting hole's
# circle or a board outline doesn't
BOARD_MARK_MARGIN_MM = 1.0
_BOARD_SILK = ("gr_circle", "gr_line", "gr_poly", "gr_rect", "gr_arc")
_ELEC_FP = re.compile(r"^(CP_Radial|CP_Elec|C_Elec)|Elec|CAP-TH", re.I)

# ------------------------------------------------------------ board side


@dataclass
class KPad:
    number: str
    x: float
    y: float
    w: float
    h: float
    function: str = ""
    angle: float = 0.0  # absolute pad angle on the board (includes fp rot)


@dataclass
class Footprint:
    ref: str
    fpid: str
    x: float
    y: float
    rot: float
    side: str
    pads: list[KPad] = field(default_factory=list)
    silk: list[list[tuple[float, float]]] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    # pin-1 mark candidates (x, y) on the part's own side's silk, local frame
    marks: list[tuple[float, float]] = field(default_factory=list)
    # the part's own side's silk drawings and "+" texts, local frame
    own_silk: list[list[tuple[float, float]]] = field(default_factory=list)
    plus_texts: list[tuple[float, float]] = field(default_factory=list)
    # the same, from silk drawn on the board by this part only (#1380)
    board_marks: list[tuple[float, float]] = field(default_factory=list)
    board_silk: list[list[tuple[float, float]]] = field(default_factory=list)
    board_plus: list[tuple[float, float]] = field(default_factory=list)
    # board silk drawings by this part and another, so counted for neither
    board_shared: int = 0

    @property
    def name(self) -> str:
        return self.fpid.split(":", 1)[-1]


def _t(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and c and _t(c[0]) == name]


def _kid(n, name):
    k = _kids(n, name)
    return k[0] if k else None


def _nums(n):
    return [float(v) for v in n[1:] if isinstance(v, (int, float))] if n else []


def _silk_shape(node) -> list[tuple[float, float]] | None:
    lay = _kid(node, "layer")
    if not lay or not str(_t(lay[1])).endswith("SilkS"):
        return None
    head = _t(node[0])
    if head == "fp_line":
        return [tuple(_nums(_kid(node, "start"))), tuple(_nums(_kid(node, "end")))]
    if head == "fp_rect":
        (x0, y0), (x1, y1) = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    if head == "fp_poly":
        pts = _kid(node, "pts")
        out = [tuple(_nums(p)) for p in _kids(pts, "xy")] if pts else []
        return out + out[:1]
    if head == "fp_circle":
        (cx, cy), (ex, ey) = _nums(_kid(node, "center")), _nums(_kid(node, "end"))
        r = math.hypot(ex - cx, ey - cy)
        return [(cx + r * math.cos(a / 12 * math.pi),
                 cy + r * math.sin(a / 12 * math.pi)) for a in range(25)]
    if head == "fp_arc":
        return [tuple(_nums(_kid(node, k))) for k in ("start", "mid", "end")]
    return None


def _pad_field(fp: Footprint) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) of the footprint's numbered pads, local frame."""
    pads = [p for p in fp.pads if p.number]
    if not pads:
        return None
    xs, ys = [p.x for p in pads], [p.y for p in pads]
    return min(xs), min(ys), max(xs), max(ys)


def _nearest_corners(x: float, y: float, box) -> set[int]:
    """The pad field's corners nearest (x, y). A point mid-side has two, and
    the centre all four: that's why this is a set."""
    x0, y0, x1, y1 = box
    d = [math.hypot(x - cx, y - cy)
         for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {i for i, v in enumerate(d) if v <= min(d) + CORNER_TIE_MM}


def _cuts_a_corner(a, b, box) -> bool:
    """A short 45 degree line square to the way out from the pad field's
    centre, so a chamfer and not a spoke or a stroke of an outline."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if not 0 < length <= CHAMFER_MAX_MM or abs(abs(dx) - abs(dy)) > 0.3 * length:
        return False
    ox = (a[0] + b[0]) / 2 - (box[0] + box[2]) / 2
    oy = (a[1] + b[1]) / 2 - (box[1] + box[3]) / 2
    out = math.hypot(ox, oy)
    return out > 0 and abs(dx * ox + dy * oy) / (length * out) < 0.5


def _own_side(node, side: str) -> bool:
    lay = _kid(node, "layer")
    return bool(lay) and str(_t(lay[1])) == ("F.SilkS" if side == "top"
                                             else "B.SilkS")


def _silk_mark(node, side: str, box) -> tuple[float, float] | None:
    """Where this drawing marks a corner, or None if it doesn't look like a
    mark. Only the part's own side counts: that's the silk JLC reads."""
    lay = _kid(node, "layer")
    if not box or not lay or str(_t(lay[1])) != ("F.SilkS" if side == "top"
                                                 else "B.SilkS"):
        return None
    head, at = _t(node[0]), None
    if head == "fp_circle":
        (cx, cy), (ex, ey) = _nums(_kid(node, "center")), _nums(_kid(node, "end"))
        if math.hypot(ex - cx, ey - cy) <= MARK_MAX_R_MM:
            at = (cx, cy)
    elif head == "fp_arc" and _kid(node, "mid"):
        # a dot: start and end meet, so it's a full circle through mid
        a, m, e = (_nums(_kid(node, k)) for k in ("start", "mid", "end"))
        if (math.hypot(a[0] - e[0], a[1] - e[1]) <= ARC_CLOSED_MM
                and math.hypot(a[0] - m[0], a[1] - m[1]) / 2 <= MARK_MAX_R_MM):
            at = ((a[0] + m[0]) / 2, (a[1] + m[1]) / 2)
    elif head in ("fp_poly", "fp_rect"):
        pts = set(_silk_shape(node) or ())
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if len(pts) > 1 and max(max(xs) - min(xs), max(ys) - min(ys)) <= MARK_MAX_SPAN_MM:
            at = (sum(xs) / len(xs), sum(ys) / len(ys))
    elif head == "fp_line":
        a, b = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        if _cuts_a_corner(a, b, box):
            at = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    # a mark dead centre points at no corner
    return at if at and len(_nearest_corners(*at, box)) < 4 else None


def _custom_copper(pad, size, local_rot: float):
    """(dx, dy, w, h) of a custom pad's copper: its area centroid from the
    anchor, turned into the footprint frame, and its bounding box in the pad's
    own frame. None when the pad has no primitives or no area."""
    import geom  # shapely: only boards with custom pads pay for it
    g = geom._custom_pad(pad, size[0], size[1])
    if g is None or g.is_empty or g.area <= 0:
        return None
    x0, y0, x1, y1 = g.bounds
    dx, dy = rot(g.centroid.x, g.centroid.y, local_rot)
    return dx, dy, x1 - x0, y1 - y0


def board_footprints(pcb: Path) -> dict[str, Footprint]:
    """{ref: Footprint} with pads and silk in the footprint's LOCAL frame."""
    tree = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    out: dict[str, Footprint] = {}
    for fp in _kids(tree, "footprint"):
        at = _nums(_kid(fp, "at"))
        props = {str(_t(p[1])): str(_t(p[2])) for p in _kids(fp, "property")
                 if len(p) > 2}
        ref = props.get("Reference", "")
        layer = str(_t(_kid(fp, "layer")[1]))
        f = Footprint(ref=ref, fpid=str(_t(fp[1])), x=at[0], y=at[1],
                      rot=at[2] if len(at) > 2 else 0.0,
                      side="bottom" if layer.startswith("B.") else "top",
                      fields=props)
        for pad in _kids(fp, "pad"):
            pat = _nums(_kid(pad, "at"))
            size = _nums(_kid(pad, "size")) or [0.0, 0.0]
            pf = _kid(pad, "pinfunction")
            px, py, w, h = pat[0], pat[1], size[0], size[1]
            if len(pad) > 3 and str(_t(pad[3])) == "custom":
                # a custom pad lands where its copper is, not at its anchor
                # (KiCad's SOT-89 tab, a QFN's 0.005 mm corner pins)
                cc = _custom_copper(pad, size, (pat[2] if len(pat) > 2 else 0.0) - f.rot)
                if cc:
                    px, py, w, h = pat[0] + cc[0], pat[1] + cc[1], cc[2], cc[3]
            f.pads.append(KPad(number=str(_t(pad[1])), x=px, y=py,
                               w=w, h=h,
                               function=str(_t(pf[1])) if pf else "",
                               angle=pat[2] if len(pat) > 2 else 0.0))
        for node in fp[1:]:
            if isinstance(node, list) and node:
                s = _silk_shape(node)
                if s:
                    f.silk.append(s)
                m = _silk_mark(node, f.side, _pad_field(f)) if s else None
                if m:
                    f.marks.append(m)
                own = _own_side(node, f.side)
                if s and own:
                    f.own_silk.append(s)
                elif own and _t(node[0]) == "fp_text" and len(node) > 2 \
                        and str(_t(node[2])).strip() == "+":
                    f.plus_texts.append(tuple(_nums(_kid(node, "at"))[:2]))
        if ref:
            out[ref] = f
    _board_silk(tree, out)
    return out


def _local_node(node, fp: Footprint):
    """A board silk drawing as the fp_ drawing it would be in fp's stored
    local frame (board = origin + rot(local, fp.rot); a bottom part's stored
    frame is already flipped), or None if it isn't one we read. A gr_rect turns
    with the part, so it comes back as a four-corner fp_poly."""
    head = _t(node[0])
    lay = _kid(node, "layer")
    if lay is None:
        return None
    loc = lambda xy: list(rot(xy[0] - fp.x, xy[1] - fp.y, -fp.rot))
    if head == "gr_text":
        at = _nums(_kid(node, "at"))
        if len(node) < 2 or str(_t(node[1])).strip() != "+" or len(at) < 2:
            return None
        return ["fp_text", "user", "+", ["at", *loc(at[:2])], lay]
    if head not in _BOARD_SILK:
        return None
    if head == "gr_rect":
        (x0, y0), (x1, y1) = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        return ["fp_poly", ["pts", *(["xy", *loc(c)] for c in
                                     ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))],
                lay]
    out = ["fp_" + head[3:]]
    for k in node[1:]:
        if isinstance(k, list) and k and _t(k[0]) in ("start", "end", "center", "mid"):
            out.append([k[0], *loc(_nums(k))])
        elif isinstance(k, list) and k and _t(k[0]) == "pts":
            out.append(["pts", *(["xy", *loc(_nums(xy))] for xy in _kids(k, "xy"))])
        else:
            out.append(k)
    return out


def _near_pads(pts, fp: Footprint) -> bool:
    """Every point within BOARD_MARK_MARGIN_MM of the part's pad copper."""
    pads = [p for p in fp.pads if p.number]
    if not pads or not pts:
        return False
    half = max(max(p.w, p.h) for p in pads) / 2 + BOARD_MARK_MARGIN_MM
    box = _pad_field(fp)
    return all(box[0] - half <= x <= box[2] + half
               and box[1] - half <= y <= box[3] + half for x, y in pts)


def _board_silk(tree, fps: dict[str, Footprint]) -> None:
    """Give each part the board-level silk drawn by it alone (#1380)."""
    for node in tree[1:]:
        if not (isinstance(node, list) and node
                and _t(node[0]) in (*_BOARD_SILK, "gr_text")):
            continue
        lay = _kid(node, "layer")
        layer = str(_t(lay[1])) if lay else ""
        if layer not in ("F.SilkS", "B.SilkS"):
            continue
        side = "top" if layer == "F.SilkS" else "bottom"
        near = []
        for f in fps.values():
            if f.side != side:
                continue
            ln = _local_node(node, f)
            if ln is None:
                break
            pts = ([tuple(_nums(_kid(ln, "at"))[:2])] if _t(ln[0]) == "fp_text"
                   else _silk_shape(ln))
            if _near_pads(pts, f):
                near.append((f, ln, pts))
        if len(near) > 1:
            for f, _, _ in near:
                f.board_shared += 1
        elif near:
            f, ln, pts = near[0]
            if _t(ln[0]) == "fp_text":
                f.board_plus.append(pts[0])
                continue
            f.board_silk.append(pts)
            m = _silk_mark(ln, f.side, _pad_field(f))
            if m:
                f.board_marks.append(m)


# ------------------------------------------------------------- geometry

def rot(x: float, y: float, deg: float) -> tuple[float, float]:
    """Turn (x, y) by deg COUNTER-CLOCKWISE as seen on screen, in a y-down
    frame (KiCad's board frame and EasyEDA's canvas)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (x * c + y * s, -x * s + y * c)


def cpl_rotation(side: str, board_rot: float, correction: float) -> float:
    """The CPL rotation for a part with this KiCad rotation and model (or
    table) correction; the bottom's term is derived in the module docstring."""
    base = board_rot if side == "top" else 180.0 - board_rot
    return (base + correction) % 360.0


def _centres(pads, key) -> dict[str, tuple[float, float]]:
    acc: dict[str, list] = {}
    for p in pads:
        k = key(p)
        if k:
            acc.setdefault(k, []).append((p.x, p.y))
    return {k: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v))
            for k, v in acc.items()}


def _fit(emap, kmap, tol) -> list[tuple[float, float]]:
    """[(correction, max error mm)] for every turn under which each shared
    key of the model lands on the same key of the board footprint."""
    common = sorted(set(emap) & set(kmap))
    if len(common) < 2:
        return []
    kc = (sum(kmap[k][0] for k in common) / len(common),
          sum(kmap[k][1] for k in common) / len(common))
    good = []
    for c in list(range(0, 360, 90)) + list(range(45, 360, 90)):
        pts = {k: rot(*emap[k], c) for k in common}
        ec = (sum(p[0] for p in pts.values()) / len(common),
              sum(p[1] for p in pts.values()) / len(common))
        err = max(math.hypot(pts[k][0] - ec[0] - (kmap[k][0] - kc[0]),
                             pts[k][1] - ec[1] - (kmap[k][1] - kc[1]))
                  for k in common)
        if err <= tol:
            good.append((float(c), err))
    if good and any(c % 90 == 0 for c, _ in good):
        good = [g for g in good if g[0] % 90 == 0]
    return good


# --------------------------------------------------------------- polarity

_ROLES = {"K": "K", "KA": "K", "CATHODE": "K", "A": "A", "ANODE": "A",
          "+": "+", "POS": "+", "PLUS": "+", "-": "-", "NEG": "-",
          "MINUS": "-"}
_POLAR_FP = re.compile(r"^(D_|LED|CP_|C_Elec)|LED|Diode|SOD-|Tantal|Elec|"
                       r"CAP-TH|CAP-SMD_BD|^CASE-[A-E]", re.I)
_POLAR_REF = re.compile(r"^(D|LED|CR)\d")


def role(name: str) -> str:
    return _ROLES.get((name or "").strip().upper(), "")


def is_polar(fp: Footprint, model=None) -> bool:
    """Diodes, LEDs and polarised caps: by pad function, refdes, the board
    footprint's name or the LCSC model's package name."""
    if _role_pair({p.number: role(p.function) for p in fp.pads}) or (
            model and _role_pair(model_roles(model))):
        return True
    return bool(_POLAR_REF.match(fp.ref) or _POLAR_FP.search(fp.name)
                or (model and _POLAR_FP.search(model.package or "")))


def board_roles(fp: Footprint, model) -> dict[str, str]:
    """pad number -> role on the board side (see the module docstring)."""
    got = {p.number: role(p.function) for p in fp.pads if role(p.function)}
    if got:
        return got
    # pads named for the pin itself (Eagle libraries: A/C, A/K): a diode's C is
    # its cathode, whatever a lone "C" means elsewhere
    named = {p.number.upper(): p.number for p in fp.pads}
    if set(named) in ({"A", "C"}, {"A", "K"}):
        return {pad: ("A" if up == "A" else "K") for up, pad in named.items()}
    n = fp.name
    if re.match(r"^(D_|LED_)", n):
        return {"1": "K", "2": "A"}
    if re.match(r"^(CP_|C_Elec)", n):
        return {"1": "+", "2": "-"}
    if model is not None and model.package and model.package in n:
        return model_roles(model)
    # After the check above: an imported footprint keeps its model's numbering.
    if n.upper().startswith("LED") and _POLAR_REF.match(fp.ref) \
            and {p.number for p in fp.pads if p.number} == {"1", "2"}:
        return {"1": "K", "2": "A"}
    return {}


def model_roles(model) -> dict[str, str]:
    return {num: role(nm) for num, nm in model.pin_names.items() if role(nm)}


def _role_pair(roles: dict[str, str]) -> bool:
    """A polarity needs both ends named: K and A, or + and -. A lone "A" is a
    switch's pin A (LCSC C318884, TS-1187A, names pin 1 A), not an anode."""
    got = set(roles.values())
    return {"K", "A"} <= got or {"+", "-"} <= got


# A keyed housing (JST, Molex, a battery holder) goes on one way: its two pads
# are pin 1 and pin 2, not either way round. oasis v0.2's mister1 (JST PH,
# C5251182, whose model numbers the pads the other way to KiCad's) went to JLC
# 180 deg off for want of this.
_KEYED_FP = re.compile(r"JST|Molex|Conn|Terminal|Battery|Header|Socket|"
                       r"^CONN-|^HDR-|^BAT|USB", re.I)
_KEYED_REF = re.compile(r"^(J|CN|P|BT|X)\d")
# An unkeyed single row (pin header or socket) is the same part turned 180.
_ROW_FP = re.compile(r"Pin(Header|Socket)_1x\d", re.I)
_SWITCH_FP = re.compile(r"^SW_|Button|Switch|^SW-|^KEY-", re.I)


def is_keyed(fp: Footprint, model=None) -> bool:
    return bool(_KEYED_REF.match(fp.ref) or _KEYED_FP.search(fp.name)
                or (model and _KEYED_FP.search(model.package or "")))


def is_switch(fp: Footprint, model=None) -> bool:
    return bool(_SWITCH_REF.match(fp.ref) or _SWITCH_FP.search(fp.name)
                or (model and _SWITCH_FP.search(model.package or "")))


# -------------------------------------------------------------- verdicts

def _ang_eq(a: float, b: float, tol: float = ROT_TOL_DEG) -> bool:
    d = (a - b) % 360.0
    return min(d, 360.0 - d) <= tol


_SWITCH_REF = re.compile(r"^(S|SW|BTN)\d")


def _geometry_fit(mpads, kpads, tol) -> list[float]:
    """Turns (multiples of 45 deg) that land every model pad on a board pad,
    one to one, numbers aside."""
    fits = []
    kc = [(p.x, p.y) for p in kpads]
    for c in range(0, 360, 45):
        placed = [rot(p.x, p.y, float(c)) for p in mpads]
        used = set()
        for x, y in placed:
            hit = next((i for i, q in enumerate(kc)
                        if i not in used and math.hypot(x - q[0], y - q[1]) <= tol), None)
            if hit is None:
                break
            used.add(hit)
        else:
            fits.append(float(c))
    return fits


def _pin_number(number: str) -> str:
    """An Eagle pad name for a pin number: P1, P$1 -> 1."""
    m = re.fullmatch(r"[Pp]\$?(\d+)", number or "")
    return m.group(1) if m else number


def analyse(fp: Footprint, model) -> dict:
    """The corrections that put this model onto this footprint, with how
    they were found. No CPL involved, so bom_cpl can derive from it."""
    if model is None:
        return {"verdict": "no_model",
                "why": "no LCSC footprint model (no LCSC number, or EasyEDA "
                       "had no data and nothing is cached)"}
    # a pad with no number is paste or mask only (a 0201's reduced paste): nothing lands on it
    kpads = [p for p in fp.pads if p.w > 0 and p.number]
    if fp.side != "top":
        # a flipped footprint's local pads are mirrored in y: undo it, so the
        # fit is the part as read from its own side
        kpads = [replace(p, y=-p.y) for p in kpads]
    # A fit holds when every model pad is nearer its own board pad than any
    # other: half the smallest gap between two numbered board pad centres.
    # Land patterns of one package differ in toe length (an SO-8 model's
    # rows 6.0 mm apart vs a KiCad 5.12), so pad size is no measure.
    kc = list(_centres(kpads, lambda p: p.number).values())
    gaps = [math.hypot(a[0] - b[0], a[1] - b[1])
            for i, a in enumerate(kc) for b in kc[i + 1:]]
    tol = max(0.2, 0.5 * min(gaps, default=0.5))
    num_fit = _fit(_centres(model.pads, lambda p: _pin_number(p.number)),
                   _centres(kpads, lambda p: _pin_number(p.number)), tol)
    if not num_fit:
        # An exposed pad numbered differently on the two sides (oasis U1:
        # the ESP32-C3-WROOM-02 model's pad 19 is nine entries, the board's
        # one corner of a 21-number grid) drags its centre off: fit again on
        # the numbers drawn as alike on both sides.
        mc = Counter(_pin_number(p.number) for p in model.pads)
        kn = Counter(_pin_number(p.number) for p in kpads)
        same = {n for n, c in mc.items() if kn.get(n) == c}
        if len(same) >= 2 and same != set(mc) & set(kn):
            num_fit = _fit(
                _centres([p for p in model.pads if _pin_number(p.number) in same],
                         lambda p: _pin_number(p.number)),
                _centres([p for p in kpads if _pin_number(p.number) in same],
                         lambda p: _pin_number(p.number)), tol)
    polar = is_polar(fp, model)
    out = {"polar": polar, "tol_mm": round(tol, 3), "basis": "pad number"}
    corr = [c for c, _ in num_fit]
    if polar:
        mr, br = model_roles(model), board_roles(fp, model)
        shared = set(mr.values()) & set(br.values())
        if shared:
            rkey_m = {n: r for n, r in mr.items()}
            role_fit = _fit(_centres([p for p in model.pads if p.number in rkey_m],
                                     lambda p: rkey_m.get(p.number)),
                            _centres([p for p in kpads if p.number in br],
                                     lambda p: br.get(p.number)), tol)
            rc = [c for c, _ in role_fit]
            if rc:
                out["basis"] = "pin role (K/A, +/-)"
                if corr and not set(rc) & set(corr):
                    # the numbers differ, the part doesn't: check_part fails
                    # a CPL that follows them
                    out["number_corrections"] = corr
                corr = rc
        else:
            # LCSC symbols for polarised caps are usually pins "1"/"2" with
            # pad 1 the + (and K for diodes), which is KiCad's numbering too.
            out["basis"] = "pad number (no K/A or +/- names on both sides)"
    elif corr and (_ROW_FP.search(fp.name) or (
            len(model.pads) == 2 and len(kpads) == 2 and not is_keyed(fp, model))):
        corr = sorted({c % 360 for c in corr} | {(c + 180) % 360 for c in corr})
        out["basis"] = "pad number, either way round (unkeyed)"
    elif is_switch(fp, model) and len(model.pads) == len(kpads) and (
            not corr or len({p.number for p in kpads}) < len(kpads)):
        # A tact switch's pin numbers carry no order (LCSC's C&K KMR2 model
        # numbers 1, 2 across the short side; the part commons the long-axis
        # pairs): what has to hold is its pads landing on the board's. The
        # pairing itself is the footprint's, not the placement's.
        corr = _geometry_fit(model.pads, kpads, tol)
        # either turn works; pin 1 on pad 1 when one does it, so the image
        # pass (red dot on cyan 1) agrees
        m1 = next((p for p in model.pads if p.number == "1"), None)
        k1 = next((p for p in kpads if _pin_number(p.number) == "1"), None)
        if corr and m1 and k1:
            corr.sort(key=lambda c: math.hypot(rot(m1.x, m1.y, c)[0] - k1.x,
                                               rot(m1.x, m1.y, c)[1] - k1.y))
        if corr:
            out["basis"] = "pad geometry (a switch's pin numbers carry no order)"
    if not corr and not polar and len(kpads) >= 3 and Counter(
            _pin_number(p.number) for p in model.pads) == Counter(
            _pin_number(p.number) for p in kpads):
        # The same pin numbers, drawn in another order (oasis Q1/Q3: a
        # SOT-23 "pinswapped" footprint numbered to its symbol, pad 2 alone
        # where the part's drain, pin 3, is). JLC places by pad geometry,
        # so a geometric fit is where the part goes; whether that permuted
        # pin map is meant is the schematic's question, not the CPL's.
        geo = _geometry_fit(model.pads, kpads, tol)
        # ...but a land pattern that is the part's mirror image (a bottom
        # footprint stored unflipped) also lands by geometry: that one is
        # no placement at all, and stays no_fit.
        # (half the fit tolerance: at a full one a three-pad SOT-23's
        # mirror "fits" a permuted one, 0.9 mm out at 0.95)
        mirrored = [c for c, err in _fit(
            {n: (-x, y) for n, (x, y) in _centres(
                model.pads, lambda p: _pin_number(p.number)).items()},
            _centres(kpads, lambda p: _pin_number(p.number)), tol)
            if err <= tol / 2]
        if geo and not mirrored:
            out.update(verdict=PIN_MAP, corrections=geo,
                       basis="pad geometry (pad numbers differ)",
                       why="the LCSC model's pads land on the board "
                           "footprint's only by geometry: the board's pad "
                           "numbers are the part's in another order, so "
                           "check the symbol-to-footprint pin map")
            return out
    if not corr:
        out.update(verdict="no_fit",
                   why="the LCSC model's pads do not land on the board "
                       "footprint's pads by number at any 45-degree turn")
        return out
    out.update(verdict="ok", corrections=corr)
    return out


def derive_rotation(fp: Footprint, model) -> float | None:
    """The CPL rotation the model says this part needs, or None."""
    a = analyse(fp, model)
    if a["verdict"] != "ok":
        return None
    return cpl_rotation(fp.side, fp.rot, a["corrections"][0])


def _offset_pads(fp: Footprint, model) -> tuple[list, list]:
    """The pads to compare for an offset: numbers with as many entries on the
    board as in the model. A thermal pad is drawn as 21 entries here and 9 there
    (ESP32-S3-WROOM-1), which pulls a plain mean of all entries off the centre
    by about 0.6 mm for a part that sits exactly on its footprint. Every pad if
    the two sides share no such number."""
    count = lambda pads: Counter(p.number for p in pads)
    mc, kc = count(model.pads), count(fp.pads)
    same = {n for n, c in mc.items() if kc.get(n) == c}
    em = [p for p in model.pads if p.number in same]
    km = [p for p in fp.pads if p.number in same]
    return (em, km) if em else (list(model.pads), list(fp.pads))


def offset_limit_mm(fp: Footprint) -> float:
    """The placement offset a part may have: OFFSET_PITCH_FRAC of its closest
    pad centres, at most OFFSET_WARN_MM. A 1206's pads are 3 mm apart, a 0.5 mm
    BGA's 0.5, and 0.24 mm off lands the first on its pads and the second not."""
    pts = [(p.x, p.y) for p in fp.pads]
    # 0.01 mm: KiCad stores pads a micron or two apart (Sensirion DFN-8's
    # -1.174999 / -1.175001), which is one pad row, not a 2 um pitch
    gaps = [d for i, a in enumerate(pts) for b in pts[i + 1:]
            if (d := math.hypot(a[0] - b[0], a[1] - b[1])) > 0.01]
    if not gaps:
        return OFFSET_WARN_MM
    return min(OFFSET_WARN_MM, OFFSET_PITCH_FRAC * min(gaps))


def _centre_delta(fp: Footprint, model, cpl_rot: float) -> tuple[float, float]:
    """Board pad centre minus model pad centre, both turned, in the board frame
    (y down), with the part's position taken as the footprint origin. On the
    bottom the model turns in the view from below, so its x comes back negated
    (the same mirror Bouni's exporter applies to a position offset)."""
    mp, bp = _offset_pads(fp, model)
    em = [(p.x, p.y) for p in mp]
    km = [(p.x, p.y) for p in bp]
    ec = rot(sum(x for x, _ in em) / len(em), sum(y for _, y in em) / len(em), cpl_rot)
    if fp.side != "top":
        ec = (-ec[0], ec[1])
    kc = rot(sum(x for x, _ in km) / len(km), sum(y for _, y in km) / len(km), fp.rot)
    return kc[0] - ec[0], kc[1] - ec[1]


def placement_offset(fp: Footprint, model, cpl_rot: float) -> tuple[float, float] | None:
    """How far the CPL position must move off the footprint origin for the
    part's own pads to land on the board's, when that is over offset_limit_mm:
    a footprint whose origin isn't the part's centre (SparkFun's USB-C and
    Qwiic: 2.8 and 1.9 mm) would otherwise be placed off its pads."""
    if model is None or analyse(fp, model)["verdict"] != "ok":
        return None
    dx, dy = _centre_delta(fp, model, cpl_rot)
    return (dx, dy) if math.hypot(dx, dy) > offset_limit_mm(fp) else None


def check_part(fp: Footprint, model, cpl: dict) -> dict:
    a = analyse(fp, model)
    row = {"ref": fp.ref, "footprint": fp.fpid,
           "lcsc": model.lcsc if model else None,
           "model_package": model.package if model else None,
           "board_rot": round(fp.rot % 360.0, 3),
           "cpl_rot": cpl["rot"], **a}
    if a["verdict"] not in ("ok", PIN_MAP):
        if "corrections" in a:
            row["expected_rot"] = [cpl_rotation(fp.side, fp.rot, c)
                                   for c in a["corrections"]]
        return row
    want = [cpl_rotation(fp.side, fp.rot, c) for c in a["corrections"]]
    row["expected_rot"] = want
    row["side"] = fp.side
    if not any(_ang_eq(cpl["rot"], w) for w in want):
        by_number = [cpl_rotation(fp.side, fp.rot, c)
                     for c in a.get("number_corrections", [])]
        if any(_ang_eq(cpl["rot"], w) for w in by_number):
            row["verdict"] = "wrong_polarity"
            row["why"] = (f"CPL rotation {cpl['rot']:g} follows the pad numbers, "
                          f"which puts the LCSC model's cathode/+ pin on the "
                          f"board's anode/- pad; it needs {want[0]:g}")
        elif a["verdict"] == PIN_MAP:
            row["verdict"] = "wrong_rotation"
            row["why"] = (f"CPL rotation {cpl['rot']:g} does not land the LCSC "
                          f"model's pads on the board's at all; by pad "
                          f"geometry it needs {want[0]:g} (and the pad numbers "
                          f"differ from the part's: check the pin map)")
        else:
            row["verdict"] = "wrong_rotation"
            row["why"] = (f"CPL rotation {cpl['rot']:g} puts pin 1 off pad 1; "
                          f"the LCSC model needs {want[0]:g}")
    # Offset: model pad centre vs board pad centre, both placed.
    dx, dy = _centre_delta(fp, model, cpl["rot"])
    row["offset_mm"] = round(math.hypot(cpl["x"] - fp.x - dx,
                                        cpl["y"] - fp.y - dy), 3)
    row["offset_limit_mm"] = round(offset_limit_mm(fp), 3)
    if row["verdict"] in ("ok", PIN_MAP) and row["offset_limit_mm"] < OFFSET_WARN_MM \
            and row["offset_mm"] > row["offset_limit_mm"]:
        row["verdict"] = "wrong_offset"
        row["why"] = (f"the CPL position is {row['offset_mm']} mm off where the "
                      f"model's pads meet the board's; this pad pitch allows "
                      f"{row['offset_limit_mm']} mm")
    return row


# ------------------------------------- two-pad part with no model (stand-in)

def two_pad_stand_in(fp: Footprint):
    """The model an unpolarised, unkeyed two-pad part would have: every
    LCSC two-pad model on file draws its pads along x, centred on the origin
    (508 of 508 cached, 2026-11-13), and either way round is the same part.
    None for any other part: a diode's or a housing's pin 1 is not in the
    shape."""
    kp = [p for p in fp.pads if p.w > 0 and p.number]
    if (len(kp) != 2 or kp[0].number == kp[1].number or is_polar(fp)
            or is_keyed(fp) or is_switch(fp) or _ROW_FP.search(fp.name)):
        return None
    if fp.side != "top":
        kp = [replace(p, y=-p.y) for p in kp]
    a, b = kp
    half = math.hypot(a.x - b.x, a.y - b.y) / 2
    if half <= 0:
        return None
    pad = lambda n, x: easyeda.Pad(number=n, x=x, y=0.0, w=a.w, h=a.h,
                                   shape="RECT")
    return easyeda.Model(lcsc="", title="two-pad stand-in", package="",
                         pads=[pad(a.number, -half), pad(b.number, half)])


def _two_pad_row(fp: Footprint, code: str, cpl: dict, row: dict) -> dict:
    """A `no_model` row with the verdict the two-pad stand-in gives."""
    stand = two_pad_stand_in(fp)
    if stand is None:
        return row
    got = check_part(fp, stand, cpl)
    got.update(lcsc=code or None, model_package=None, basis=TWO_PAD_BASIS)
    if got["verdict"] == "ok":
        got["why"] = (f"no LCSC model: CPL rotation {cpl['rot']:g} lays the "
                      f"part along its pads, as every LCSC two-pad model is drawn")
    elif got["verdict"] == "wrong_rotation":
        got["why"] = (f"CPL rotation {cpl['rot']:g} lays the part across its "
                      f"pads; there is no LCSC model, and every LCSC two-pad "
                      f"model is drawn along its pads, so it needs "
                      f"{got['expected_rot'][0]:g}")
    return got


# ---------------------------------------- ball grid with no model (basis footprint)

_BALL = re.compile(r"^([A-Z]+)(\d+)$")
_SQUARE_TOL = math.sin(math.radians(ROT_TOL_DEG))


def _balls(fp: Footprint) -> list[tuple[str, int, KPad]]:
    return [(m.group(1), int(m.group(2)), p) for p in fp.pads
            if (m := _BALL.match(p.number or ""))]


def bga_pin1(fp: Footprint) -> KPad | None:
    """The board's BGA pin 1: the pad named A1, else the lowest row letter and
    then the lowest column number among ball-named pads. None when the
    footprint has no ball grid (three or more rows of ball names)."""
    balls = _balls(fp)
    # a USB-C (A1..A12, B1..B12) is two rows of pins, not a ball grid
    if len(balls) < 6 or len({b[0] for b in balls}) < 3:
        return None
    for row, col, p in balls:
        if (row, col) == ("A", 1):
            return p
    return min(balls, key=lambda b: (len(b[0]), b[0], b[1]))[2]


def silk_mark(fp: Footprint) -> dict:
    """_silk_mark_of the part's own silk; where that has no mark, of the
    silk drawn on the board by it alone, with `source: board` (#1380)."""
    mark = _silk_mark_of(fp)
    if mark["state"] in (None, "none") and (fp.board_marks or fp.board_silk
                                            or fp.board_plus):
        alt = _silk_mark_of(replace(fp, marks=fp.board_marks,
                                    own_silk=fp.board_silk,
                                    plus_texts=fp.board_plus))
        if alt["state"] in ("ok", "wrong"):
            return {**alt, "source": "board"}
    return mark


def _silk_mark_of(fp: Footprint) -> dict:
    """Whether the part's pin-1 silk mark is at the board's pin 1. `state` is
    None for parts the rule leaves alone (only ball grids and U/IC/Q parts
    with three or more pads and a pin 1, a Q only when it has a mark, and the
    two-pad polar parts polarity_mark() judges), else "none", "wrong" (any mark off pin
    1's corner: a second dot leaves JLC two to choose from) or "ok". Pads and
    silk share the stored local frame, so a flip or a turn needs no transform;
    the CPL rotation is proved against the same pad 1 by this module's fit, so
    a mark at that pad's corner agrees with a CPL that passes."""
    # footprint_a1, not bga_pin1 alone: a 4-layer USB-C's pads are named like
    # balls in three rows, but they're slots and footprint_a1 knows it
    pin = bga_pin1(fp) if footprint_a1(fp) else None
    q = bool(_TRANSISTOR_REF.match(fp.ref))
    if pin is None and (_MARK_REF.match(fp.ref) or q) and len(
            {p.number for p in fp.pads if p.number}) >= 3:
        pin = next((p for p in fp.pads if p.number == "1"), None)
    if pin is None:
        return polarity_mark(fp)
    if q and not fp.marks:
        return {"state": None}
    box = _pad_field(fp)
    want = _nearest_corners(pin.x, pin.y, box)
    off = [m for m in fp.marks if not _nearest_corners(*m, box) & want]
    return {"state": "none" if not fp.marks else "wrong" if off else "ok",
            "pin": pin.number, "off": off}


def _plus_signs(fp: Footprint, ux: float, uy: float) -> list[tuple[float, float]]:
    """Where the part's own silk draws a "+": a text, or two short lines of
    about one length crossing at their middles, one along the pad axis."""
    lines = [(s[0], s[1]) for s in fp.own_silk if len(s) == 2]
    out = list(fp.plus_texts)
    for i, (a, b) in enumerate(lines):
        la = math.dist(a, b)
        if not PLUS_MIN_MM <= la <= PLUS_MAX_MM:
            continue
        for c, d in lines[i + 1:]:
            lc = math.dist(c, d)
            if not PLUS_MIN_MM <= lc <= PLUS_MAX_MM or max(la, lc) > 1.6 * min(la, lc):
                continue
            va = ((b[0] - a[0]) / la, (b[1] - a[1]) / la)
            vc = ((d[0] - c[0]) / lc, (d[1] - c[1]) / lc)
            if abs(va[0] * vc[0] + va[1] * vc[1]) > 0.2 or max(
                    abs(va[0] * ux + va[1] * uy), abs(vc[0] * ux + vc[1] * uy)) < 0.95:
                continue
            ma = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            mc = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
            if math.dist(ma, mc) <= 0.2 * min(la, lc):
                out.append(((ma[0] + mc[0]) / 2, (ma[1] + mc[1]) / 2))
    return out


def polarity_mark(fp: Footprint) -> dict:
    """silk_mark for a two-pad diode, LED or polarised cap (#800): whether its
    cathode bar or "+" is at the board's K or + pad. A "+" decides it where
    there is one; else, but for electrolytics, the silk's length-weighted
    centre along the pad axis must sit toward that pad: the centre of the
    strokes across the axis (a bar, an LED's flat) where there are any, else
    of all of it. A centred outline, or all of it leaning the other way, is
    "none". Keyed housings
    (a battery holder) and switches are left alone."""
    nums = {p.number for p in fp.pads if p.number}
    if len(nums) != 2 or not is_polar(fp) or is_keyed(fp) or is_switch(fp):
        return {"state": None}
    roles = board_roles(fp, None)
    mark = next((n for n, r in roles.items() if r in ("K", "+")), None)
    other = next((n for n, r in roles.items() if r in ("A", "-")), None)
    if not mark or not other:
        return {"state": None}

    def at(num):
        ps = [p for p in fp.pads if p.number == num]
        return (sum(p.x for p in ps) / len(ps), sum(p.y for p in ps) / len(ps))
    (mx, my), (ox, oy) = at(mark), at(other)
    half = math.hypot(mx - ox, my - oy) / 2
    if half <= 0:
        return {"state": None}
    ux, uy = (mx - ox) / (2 * half), (my - oy) / (2 * half)
    cx, cy = (mx + ox) / 2, (my + oy) / 2
    share = lambda x, y: ((x - cx) * ux + (y - cy) * uy) / half
    row = {"pin": mark, "basis": "polarity"}
    plus = [(round(x, 3), round(y, 3)) for x, y in _plus_signs(fp, ux, uy)
            if abs(share(x, y)) >= POLAR_MARK_SHARE]
    if plus:
        off = [p for p in plus if share(*p) < 0]
        return {**row, "state": "wrong" if off else "ok", "off": off,
                "why": (f"the \"+\" silk at {_pts(off)} sits by pad {other}, "
                        f"the board's {roles[other]} pad, not pad {mark}")}
    if _ELEC_FP.search(fp.name):
        return {"state": None}
    segs = [(a, b) for s in fp.own_silk for a, b in zip(s, s[1:])
            if math.dist(a, b) > 0]
    # an outline's cut corners (EasyEDA's LEDs and diodes) are at the K end,
    # and outweigh its strokes across: the cut end's side is the shorter one.
    # A circle or arc (a dot, a round body) is drawn in many points: not a cut.
    cut = [(a, b) for s in fp.own_silk if len(s) <= 8
           for a, b in zip(s, s[1:]) if math.dist(a, b) > 0
           and 0.3 <= abs((b[0] - a[0]) * ux + (b[1] - a[1]) * uy)
           / math.dist(a, b) <= 0.95
           and abs(share((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) >= CHAMFER_SHARE]
    ends = {share((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) > 0 for a, b in cut}
    if len(cut) >= 2 and len(ends) == 1:
        if ends == {True}:
            return {**row, "state": "ok", "off": []}
        where = [(round(sum(a[0] + b[0] for a, b in cut) / (2 * len(cut)), 3),
                  round(sum(a[1] + b[1] for a, b in cut) / (2 * len(cut)), 3))]
        return {**row, "state": "wrong", "off": where,
                "why": (f"its outline's cut corners are at pad {other}, the "
                        f"board's {roles[other]} pad, not pad {mark} (centre "
                        f"{_pts(where)})")}
    across = [(a, b) for a, b in segs
              if abs((b[0] - a[0]) * ux + (b[1] - a[1]) * uy) / math.dist(a, b) < 0.3]
    shares, where = [], None
    for group in (segs, across):
        length = sum(math.dist(a, b) for a, b in group)
        if length:
            gx = sum(math.dist(a, b) * (a[0] + b[0]) / 2 for a, b in group) / length
            gy = sum(math.dist(a, b) * (a[1] + b[1]) / 2 for a, b in group) / length
            shares.append(share(gx, gy))
            where = (round(gx, 3), round(gy, 3))
    # the strokes across the axis decide (a round LED's flat is lost in its
    # circle); the whole silk saying the opposite makes it unclear
    lead = shares[-1] if shares else 0.0
    if any(lead * v < 0 and abs(v) >= POLAR_MARK_SHARE for v in shares):
        return {**row, "state": "none", "off": []}
    if lead <= -POLAR_MARK_SHARE:
        return {**row, "state": "wrong", "off": [where],
                "why": (f"its silk is drawn toward pad {other}, the board's "
                        f"{roles[other]} pad, not pad {mark} (centre "
                        f"{_pts([where])})")}
    if lead >= POLAR_MARK_SHARE:
        return {**row, "state": "ok", "off": []}
    return {**row, "state": "none", "off": []}


def _pts(pts) -> str:
    return ", ".join(f"({x:g}, {y:g})" for x, y in pts)


def _step(lines: dict) -> tuple[float, float] | None:
    """The median step per index along lines of (index, x, y). Depopulated
    gaps divide out, and a stray pad named like a ball can't move a median."""
    steps = []
    for line in lines.values():
        line = sorted(line)
        steps += [((x1 - x0) / (j - i), (y1 - y0) / (j - i))
                  for (i, x0, y0), (j, x1, y1) in zip(line, line[1:]) if j != i]
    if not steps:
        return None
    return (statistics.median(s[0] for s in steps),
            statistics.median(s[1] for s in steps))


def footprint_a1(fp: Footprint) -> dict | None:
    """Where a standard part's A1 goes, from the footprint's ball names alone.

    The standard part has A1 upper left, columns running +x and rows +y (y
    down). The turn that brings its column axis onto the footprint's is the
    correction. Reading the axes, not the A1 ball's corner, handles a missing
    A1, a depopulated centre and a rectangular body. Returns None for anything
    but a ball grid, {"why"} when the names give no A1, else {"correction",
    "pin", "at"}, with `at` the pin 1 ball in the standard part's frame."""
    pin = bga_pin1(fp)
    if pin is None:
        return None
    balls = _balls(fp)
    aspects = [max(p.w, p.h) / max(min(p.w, p.h), 1e-9) for _, _, p in balls]
    if statistics.median(aspects) > bgalib.MAX_BALL_ASPECT:
        return None
    flip = 1.0 if fp.side == "top" else -1.0  # a bottom footprint is stored mirrored
    ranks = {r: i for i, r in enumerate(sorted({b[0] for b in balls},
                                               key=lambda r: (len(r), r)))}
    by_row: dict = {}
    by_col: dict = {}
    for row, col, p in balls:
        by_row.setdefault(row, []).append((col, p.x, flip * p.y))
        by_col.setdefault(col, []).append((ranks[row], p.x, flip * p.y))
    across, down = _step(by_row), _step(by_col)
    if across is None or down is None:
        return None
    la, ld = math.hypot(*across), math.hypot(*down)
    if min(la, ld) <= 0 or max(la, ld) > bgalib.MAX_PITCH + 1e-9:
        return None
    dot = (across[0] * down[0] + across[1] * down[1]) / (la * ld)
    cross = (across[0] * down[1] - across[1] * down[0]) / (la * ld)
    if abs(dot) > _SQUARE_TOL:
        return {"why": "its ball rows and columns aren't square to each other, "
                       "so the names give no A1 corner"}
    if cross < 0:
        return {"why": "its ball names run mirrored (drawn in bottom view?), so "
                       "no turn puts a standard part on it"}
    c = round(math.degrees(math.atan2(-across[1], across[0])), 3) % 360.0
    return {"correction": c, "pin": pin.number,
            "at": rot(pin.x, flip * pin.y, -c)}


def footprint_basis(fp: Footprint, code: str, fetch: bool) -> dict | None:
    """footprint_a1 for a part with no model, unless a fetch could still get
    one. Callers pass fetch=True once easyeda.get returned None with no error
    (EasyEDA answered "no model", now or kept as `<LCSC>.none`); an LCSC
    number left uncached under --offline is `fetch_failed` before this."""
    return footprint_a1(fp) if fetch or not code else None


def _footprint_row(fp: Footprint, cpl: dict, row: dict, a1: dict | None) -> dict:
    """A `no_model` row with the verdict the footprint's own A1 gives."""
    if a1 is None:
        return row
    if "why" in a1:
        return {**row, "why": f"no LCSC model, and {a1['why']}"}
    want = cpl_rotation(fp.side, fp.rot, a1["correction"])
    row = {**row, "basis": "footprint", "side": fp.side, "pin1": a1["pin"],
           "corrections": [a1["correction"]], "expected_rot": [want]}
    if _ang_eq(cpl["rot"], want):
        row["verdict"] = FOOTPRINT_MATCH
        row["why"] = (f"no LCSC model: CPL rotation {cpl['rot']:g} puts a standard "
                      f"part's {a1['pin']} on the footprint's, by convention only")
    else:
        row["verdict"] = "wrong_rotation"
        row["why"] = (f"no LCSC model: CPL rotation {cpl['rot']:g} puts a standard "
                      f"part's A1 corner off the footprint's {a1['pin']}; it needs "
                      f"{want:g}. Give the part an LCSC number or a "
                      f"jlc_rotations.csv row and rerun bom_cpl")
    return row


# ------------------------------------------------------------------- run

# A CPL's columns, and those of KiCad's own placement file (kicad-cli pcb
# export pos --format csv: Ref,Val,Package,PosX,PosY,Rot,Side), matched
# without regard to case or spaces round them.
CPL_COLUMNS = ("Designator", "Mid X", "Mid Y", "Rotation")
POS_COLUMNS = ("Ref", "PosX", "PosY", "Rot", "Side")
# KiCad's placement file with its position and rotation columns renamed to
# the CPL's (Ref,Val,Package,Mid X,Mid Y,Rotation,Side: one-rom fire-28):
# Ref and Side say it is still KiCad's file, so its rotation is KiCad's.
POS_ALIASES = {"PosX": "Mid X", "PosY": "Mid Y", "Rot": "Rotation"}
POS_NOTE = ("KiCad's placement file: each Rot is the board's own rotation, "
            "read as the CPL rotation it stands for with no correction (r on "
            "the top, 180 - r on the bottom), so wrong_rotation marks a part "
            "whose LCSC model needs a correction that a CPL would carry and "
            "this file does not")


def cpl_format(header) -> str:
    """'cpl' or 'kicad_pos' from a CSV's heading row; neither is a
    ValueError that names both column sets."""
    cols = {str(h).strip().lower() for h in header or () if h}
    if {c.lower() for c in CPL_COLUMNS} <= cols:
        return "cpl"
    if all(c.lower() in cols or POS_ALIASES.get(c, "").lower() in cols
           for c in POS_COLUMNS):
        return "kicad_pos"
    raise ValueError(
        f"not a CPL ({', '.join(CPL_COLUMNS)}, Layer) nor KiCad's placement "
        f"file ({', '.join(POS_COLUMNS)}): its columns are "
        f"{', '.join(sorted(cols)) or 'none'}")


def read_placement(path: Path) -> tuple[str, dict[str, dict]]:
    """(format, {ref: {x, y (board frame, y down), rot, layer}}) from a CPL
    or from KiCad's own placement file, also with its PosX/PosY/Rot named
    Mid X/Mid Y/Rotation (POS_ALIASES). A byte order mark is read past
    (Fabrication Toolkit writes one: oasis v0.2).

    The placement file's PosX/PosY are the CPL's Mid X/Mid Y (bom_cpl copies
    them across, y up). Its Rot is the board's KiCad rotation r on either
    side, so the row is taken as the CPL rotation r stands for with no
    correction, cpl_rotation(side, r, 0): POS_NOTE says what a verdict on it
    means."""
    out: dict[str, dict] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        fmt = cpl_format(rd.fieldnames)
        col = {str(h).strip().lower(): h for h in rd.fieldnames if h}

        def cell(r: dict, key: str, default: str = "") -> str:
            name = col.get(key.lower())
            if name is None and fmt == "kicad_pos" and key in POS_ALIASES:
                name = col.get(POS_ALIASES[key].lower())
            v = r.get(name) if name is not None else None
            return default if v is None else str(v).strip()

        for r in rd:
            if fmt == "cpl":
                out[cell(r, "Designator")] = {
                    "x": float(cell(r, "Mid X")),
                    "y": -float(cell(r, "Mid Y")),
                    "rot": float(cell(r, "Rotation")) % 360.0,
                    "layer": cell(r, "Layer", "Top")}
                continue
            side = ("bottom" if cell(r, "Side").lower() in ("bottom", "back")
                    else "top")
            out[cell(r, "Ref")] = {
                "x": float(cell(r, "PosX")), "y": -float(cell(r, "PosY")),
                "rot": cpl_rotation(side, float(cell(r, "Rot")), 0.0),
                "layer": side.capitalize()}
    return fmt, out


def read_cpl(path: Path) -> dict[str, dict]:
    """CPL.csv (or KiCad's placement file) -> {ref: {x, y (board frame,
    y down), rot, layer}}: read_placement without the format."""
    return read_placement(path)[1]


# ------------------------------------------- the assembler's convention

# A CPL's rotation means what the assembler it goes to says it means
# (#1068): JLC places by its own LCSC model (everything above), the KiCad
# shops (PCBWay, Aisler, Seeed, Eurocircuits) take KiCad's own position,
# side and rotation as exported, and a fab with no assembly service has no
# convention to judge a CPL by at all.
KICAD_BASIS = "kicad"
NOT_JUDGED = "not_judged"
POS_TOL_MM = 0.01   # vendor.py writes the pos file's mm to 4 places
ZERO_NOTE = ("{name} places from {zero}, so a KiCad rotation of a part with "
             "more than two pads can be 90 degrees off its zero: not judged "
             "here; check each in its placement review (ORDER.md)")


def convention(vendor: str | None) -> dict:
    """The vendor profile's assembly rotation convention: {"vendor", "name",
    "rotation": "jlc" | "kicad" | None, "zero_orientation", "why"}. None
    when the profile has no assembly table or no PCBA service (OSH Park,
    Advanced Circuits): a CPL for it is not judged."""
    import vendors
    vid = vendor or vendors.DEFAULT
    prof = vendors.load(vid)
    meta = prof.get("meta") or {}
    asm = prof.get("assembly") or {}
    pcba = bool((meta.get("services") or {}).get("pcba"))
    rot = asm.get("rotation") if pcba else None
    name = meta.get("name") or vid
    why = None
    if rot not in ("jlc", KICAD_BASIS):
        why = (f"{name} has no assembly table in its vendor profile "
               f"(reference/vendors/{vid}.yaml): no rotation convention to "
               f"judge a CPL by" if not (pcba and asm) else
               f"{name}'s assembly.rotation is {rot!r}, not jlc or kicad")
        rot = None
    return {"vendor": vid, "name": name, "rotation": rot,
            "zero_orientation": asm.get("zero_orientation") if rot else None,
            "why": why}


def read_vendor_cpl(path: Path) -> dict[str, dict]:
    """A KiCad-convention vendor's CPL (vendor.py package, any of the
    profiles' column sets, or KiCad's own pos file) -> {ref: {x, y (board
    frame, y down), rot (as written: KiCad's, on either side), side}}."""
    from vendor import meaning
    out: dict[str, dict] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        col: dict[str, str] = {}
        for h in rd.fieldnames or ():
            m = meaning(h or "")
            if m and m not in col:
                col[m] = h
        need = [m for m in ("designator", "x", "y", "rotation") if m not in col]
        if need:
            raise ValueError(f"{path}: no {', '.join(need)} column "
                             f"(columns: {', '.join(rd.fieldnames or [])})")
        for r in rd:
            ref = str(r.get(col["designator"]) or "").strip()
            if not ref:
                continue
            side = str(r.get(col.get("side", ""), "") or "top").strip().lower()
            out[ref] = {
                "x": float(r[col["x"]]), "y": -float(r[col["y"]]),
                "rot": float(r[col["rotation"]]) % 360.0,
                "side": ("bottom" if side in ("bottom", "back", "b",
                                              "bottomlayer", "bot")
                         else "top")}
    return out


def _kicad_row(fp: Footprint, row: dict, conv: dict) -> dict:
    """One CPL row against its own board footprint, KiCad's way: the row's
    position, side and rotation must be the footprint's."""
    want = fp.rot % 360.0
    out = {"ref": fp.ref, "footprint": fp.name, "basis": KICAD_BASIS,
           "side": fp.side, "cpl_rot": round(row["rot"], 3),
           "expected_rot": [round(want, 3)], "silk_mark": None}
    off = math.hypot(row["x"] - fp.x, row["y"] - fp.y)
    if row["side"] != fp.side:
        out.update(verdict="wrong_side",
                   why=f"the CPL puts it on the {row['side']}; the board has "
                       f"it on the {fp.side}")
    elif off > POS_TOL_MM:
        out.update(verdict="wrong_position", offset_mm=round(off, 3),
                   why=f"the CPL position is {off:.3f} mm from the board "
                       f"footprint's ({fp.x:g}, {-fp.y:g})")
    elif conv.get("zero_orientation") and len(fp.pads) > 2:
        out.update(verdict=NOT_JUDGED, why=ZERO_NOTE.format(
            name=conv["name"], zero=conv["zero_orientation"]))
    elif _ang_eq(row["rot"], want):
        out.update(verdict="ok")
    elif (len(fp.pads) == 2 and not is_polar(fp) and not is_keyed(fp)
          and _ang_eq(row["rot"], want + 180.0)):
        # an unpolarised two-pad part is the same part either way round
        out.update(verdict="ok", expected_rot=[round(want, 3),
                                               round((want + 180) % 360, 3)])
    else:
        out.update(verdict="wrong_rotation",
                   why=f"{conv['name']} takes KiCad's own rotation, "
                       f"{want:g}; the CPL says {row['rot']:g}")
    return out


def verify_vendor(pcb: Path, cpl: dict[str, dict], conv: dict,
                  fps: dict[str, Footprint] | None = None) -> dict:
    """The placement check for a vendor that is not JLC: its convention
    (convention()) decides. kicad: each row's position, side and rotation
    against its own footprint's (no LCSC model; an IEC zero_orientation
    leaves multi-pin parts not_judged). None: every row not_judged."""
    fps = fps if fps is not None else board_footprints(pcb)
    folded = {r.upper(): r for r in fps}
    rows = []
    for ref in sorted(cpl, key=_natural):
        board_ref = ref if ref in fps else folded.get(ref.upper(), ref)
        fp = fps.get(board_ref)
        if conv["rotation"] is None:
            rows.append({"ref": board_ref, "verdict": NOT_JUDGED,
                         "silk_mark": None, "why": conv["why"]})
        elif fp is None:
            rows.append({"ref": ref, "verdict": "no_fit", "silk_mark": None,
                         "why": "CPL designator is not on the board"})
        else:
            rows.append(_kicad_row(fp, cpl[ref], conv))
    bad = [r for r in rows if r["verdict"] in FAIL_VERDICTS]
    unjudged = [r["ref"] for r in rows if r["verdict"] == NOT_JUDGED]
    return {"script": "cpl_verify", "board": Path(pcb).name,
            "vendor": conv["vendor"], "basis": conv["rotation"] or NOT_JUDGED,
            "ledger": None,
            "status": ("violations" if bad else
                       NOT_JUDGED if conv["rotation"] is None else "pass"),
            "n_checked": len(rows), "n_failed": len(bad),
            "failed": [r["ref"] for r in bad],
            "not_judged": ({"refs": unjudged,
                            "note": conv["why"] or next(
                                r["why"] for r in rows
                                if r["verdict"] == NOT_JUDGED)}
                           if unjudged else None),
            "bottom_preview": None, "footprint_basis": None,
            "pin_numbers_differ": None, "silk_mark_wrong": [],
            "silk_mark_missing": None, "offset_warnings": [],
            "fetch_failed": [], "rate_limited": None, "cache_dir": None,
            "parts": rows}


def offline() -> bool:
    return os.environ.get("HWDE_EASYEDA_OFFLINE", "") not in ("", "0")


def default_cache(parts_json: Path | None) -> Path:
    import env
    return env.easyeda_cache(
        Path(parts_json).parent if parts_json is not None else None)


def lcsc_map(pcb: Path, parts_json: Path | None) -> dict[str, str]:
    import bom_cpl
    m = {r: f["lcsc"] for r, f in bom_cpl.board_part_fields(pcb).items()
         if f.get("lcsc")}
    m.update({r: e["lcsc"] for r, e in bom_cpl.load_parts_map(parts_json).items()
              if e.get("lcsc")})
    return m


def verify(pcb: Path, cpl: dict[str, dict], lcsc: dict[str, str],
           cache_dir: Path, fetch: bool = True,
           fps: dict[str, Footprint] | None = None,
           unsourced_placed: bool = True) -> dict:
    """unsourced_placed=False: a row with no LCSC number is a part JLC does
    not place (a foreign board's BOM line with no part: oasis v0.2's D1-D5,
    hand-fitted LEDs), so it is `not_placed`, not `no_model`. hwde's own CPL
    holds only smt_placed parts, so there it stays a failure."""
    fps = fps if fps is not None else board_footprints(pcb)
    # Exporters upper-case designators (Fabrication Toolkit: oasis's mister1
    # ships as MISTER1) and JLC matches them so; an exact match comes first.
    folded = {r.upper(): r for r in fps}
    rows = []
    for ref in sorted(cpl, key=_natural):
        board_ref = ref if ref in fps else folded.get(ref.upper(), ref)
        fp = fps.get(board_ref)
        if fp is None:
            rows.append({"ref": ref, "verdict": "no_fit",
                         "why": "CPL designator is not on the board"})
            continue
        code = lcsc.get(board_ref, "")
        if not code and not unsourced_placed:
            rows.append({"ref": board_ref, "footprint": fp.name, "lcsc": None,
                         "verdict": "not_placed", "silk_mark": None,
                         "why": "no LCSC number on the board: JLC does not "
                                "place it"})
            continue
        errors: dict = {}
        model = easyeda.get(code, cache_dir, fetch=fetch, pace_s=1.0,
                            errors=errors)
        row = check_part(fp, model, cpl[ref])
        if row["verdict"] == "no_model" and errors:
            row["verdict"] = "fetch_failed"
            row["fetch_error"] = next(iter(errors.values()))
            row["why"] = (f"could not fetch the LCSC model for {code} "
                          f"({row['fetch_error']}); cache dir "
                          f"{cache_dir} - this is not a pin-1 result")
        elif row["verdict"] == "no_model":
            # no error: EasyEDA answered "no model" (now, or a kept .none
            # under --offline); an offline miss is an error, above
            row = _footprint_row(fp, cpl[ref], row,
                                 footprint_basis(fp, code, True))
            if row["verdict"] == "no_model":
                row = _two_pad_row(fp, code, cpl[ref], row)
        mark = silk_mark(fp)
        row["silk_mark"] = mark["state"]
        if mark["state"]:
            row["silk_pin"] = mark["pin"]
            row["silk_off"] = mark["off"]
            if mark.get("why"):
                row["silk_why"] = mark["why"]
            if mark.get("source"):
                row["silk_source"] = mark["source"]
        if fp.board_shared and mark["state"] == "none":
            # only where a board mark could have answered: else it's noise
            # (glasgow's 0402s sit 234 deep in shared drawings)
            row["silk_note"] = (f"{fp.board_shared} silk drawing(s) on the "
                                f"board sit by this part and another, so they "
                                f"count for neither")
        rows.append(row)
    bad = [r for r in rows if r["verdict"] in FAIL_VERDICTS]
    below = [r["ref"] for r in rows if r.get("side") == "bottom"
             and r["verdict"] not in FAIL_VERDICTS and not _either_way(r)]
    by_convention = [r["ref"] for r in rows if r["verdict"] == FOOTPRINT_MATCH]
    pin_map = [r["ref"] for r in rows if r["verdict"] == PIN_MAP]
    no_mark = [r["ref"] for r in rows if r.get("silk_mark") == "none"]
    wrong_mark = [r["ref"] for r in rows if r.get("silk_mark") == "wrong"]
    led = record_footprints(pcb, rows, lcsc)
    return {"script": "cpl_verify", "board": Path(pcb).name,
            "ledger": led,
            "status": "violations" if bad or wrong_mark else "pass",
            "n_checked": len(rows), "n_failed": len(bad),
            "failed": [r["ref"] for r in bad],
            "bottom_preview": ({"refs": below, "note": BOTTOM_PREVIEW_NOTE}
                               if below else None),
            "footprint_basis": ({"refs": by_convention, "note": FOOTPRINT_BASIS_NOTE}
                                if by_convention else None),
            "pin_numbers_differ": ({"refs": pin_map, "note": PIN_MAP_NOTE}
                                   if pin_map else None),
            "silk_mark_wrong": wrong_mark,
            "silk_mark_missing": ({"refs": no_mark, "note": NO_SILK_MARK_NOTE}
                                  if no_mark else None),
            "offset_warnings": [r["ref"] for r in rows
                                if r.get("offset_mm", 0) > r.get("offset_limit_mm", OFFSET_WARN_MM)
                                and r["verdict"] in ("ok", PIN_MAP)],
            "fetch_failed": [r["ref"] for r in rows
                             if r["verdict"] == "fetch_failed"],
            "rate_limited": easyeda.rate_limited(),
            "cache_dir": str(cache_dir),
            "parts": rows}


# A footprint whose pads land on the LCSC model by number is the part's land
# pattern whatever the CPL turn says (a wrong rotation or a K on A is the CPL's fault).
FOOTPRINT_OK = ("ok", "wrong_rotation", "wrong_polarity", "wrong_offset")


def _either_way(row: dict) -> bool:
    """Every rotation the row accepts is also accepted turned 180: the part
    is right under either bottom-side reading."""
    want = row.get("expected_rot") or []
    return bool(want) and all(any(_ang_eq(w + 180.0, x) for x in want)
                              for w in want)


def record_footprints(pcb: Path, rows: list[dict],
                      lcsc: dict[str, str]) -> str | None:
    """Each checked part's `footprint` fact into the workspace ledger
    (lib/ledgerlib.py): verified when the board pads land on the LCSC
    model's by number, failed when they do not, unknown with no model."""
    import ledgerlib
    ws = ledgerlib.workspace_of(pcb)
    if not ledgerlib.writable(ws):
        return None
    facts = []
    for r in rows:
        code = lcsc.get(r["ref"], "")
        v = r.get("verdict")
        # footprint basis: there was no model for the pads to land on
        st = ("verified" if v in FOOTPRINT_OK
              and r.get("basis") not in ("footprint", TWO_PAD_BASIS) else
              "failed" if v == "no_fit" else "unknown")
        facts.append({
            "subject": r["ref"], "kind": "footprint", "status": st,
            "lcsc": code,
            "value": (f"board pads land on the LCSC model's by "
                      f"{r.get('basis', 'pad number')}" if st == "verified"
                      else r.get("why") or v),
            "source": (f"EasyEDA/LCSC footprint model of {code}"
                       if st != "unknown" else "")})
    exp = ledgerlib.expected(ws)
    out = ledgerlib.put(ws, [f for f in facts if ledgerlib.key(
        f["subject"], "footprint") in exp], "cpl_verify")
    return str(out) if out else None


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


# ------------------------------------------------------- visual verdicts

VISUAL_OK = "match"


def merge_visual(parts: list[dict], visual: dict | None) -> list[dict]:
    """Put the image verdict beside the script verdict, per designator.

    `visual` is cpl_visual.json: {"model": ..., "parts": {ref: {"pin1":
    "match"|"mismatch"|"unclear", "polarity": "match"|"mismatch"|"n/a"|
    "unclear", "note": str}}}. A ref is `agree` when both say right or both
    say wrong, `disagree` otherwise; `unclear` and `missing` (the visual pass
    has no line for a checked part) are not agreement either. Returns the
    rows with `visual` and `agreement` set."""
    vparts = (visual or {}).get("parts", {})
    out = []
    for r in parts:
        r = dict(r)
        v = vparts.get(r["ref"])
        r["visual"] = v
        if visual is None:
            r["agreement"] = "not_run"
        elif v is None:
            r["agreement"] = "missing"
        else:
            marks = [v.get("pin1"), v.get("polarity")]
            if "unclear" in marks:
                r["agreement"] = "unclear"
            else:
                vis_ok = all(m in (VISUAL_OK, "n/a", None) for m in marks)
                scr_ok = r["verdict"] in ("ok", FOOTPRINT_MATCH, PIN_MAP)
                r["agreement"] = "agree" if vis_ok == scr_ok else "disagree"
        out.append(r)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--cpl", required=True,
                    help="CPL.csv to verify, or KiCad's placement file "
                         "(kicad-cli pcb export pos --format csv)")
    ap.add_argument("--parts", help="parts.json (LCSC numbers per ref)")
    ap.add_argument("--cache-dir", help="EasyEDA model cache directory")
    ap.add_argument("--offline", action="store_true",
                    help="never fetch: an uncached part is fetch_failed "
                         "(offline), one kept as <LCSC>.none is no model")
    ap.add_argument("--visual", help="cpl_visual.json from the image pass")
    ap.add_argument("--vendor", help="the assembler the CPL goes to "
                    "(reference/vendors/<id>.yaml); its assembly.rotation "
                    "decides how rotation is judged. Default: the "
                    "workspace's chosen vendor (vendor.py choose), else JLC")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)
    try:
        pcb = Path(args.pcb)
        pj = Path(args.parts) if args.parts else None
        vid = args.vendor
        if not vid:
            import ledgerlib
            ws = ledgerlib.workspace_of(pcb)
            vid = ledgerlib.chosen_vendor(ws) if ws else None
        conv = convention(vid)
        if conv["rotation"] != "jlc":
            rep = verify_vendor(pcb, read_vendor_cpl(Path(args.cpl))
                                if conv["rotation"] else
                                read_placement(Path(args.cpl))[1], conv)
            text = json.dumps(rep, indent=1)
            (Path(args.out).write_text(text, encoding="utf-8") if args.out
             else print(text))
            return {"pass": 0, "violations": 1, NOT_JUDGED: 3}[rep["status"]]
        fmt, rows = read_placement(Path(args.cpl))
        rep = verify(pcb, rows, lcsc_map(pcb, pj),
                     Path(args.cache_dir) if args.cache_dir
                     else default_cache(pj),
                     fetch=not (args.offline or offline()))
        if fmt == "kicad_pos":
            rep["cpl_format"], rep["cpl_note"] = fmt, POS_NOTE
        if args.visual:
            vis = json.loads(Path(args.visual).read_text(encoding="utf-8"))
            rep["parts"] = merge_visual(rep["parts"], vis)
            dis = [r["ref"] for r in rep["parts"]
                   if r["agreement"] in ("disagree", "missing")]
            rep["visual_disagree"] = dis
            if dis:
                rep["status"] = "violations"
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        rep = {"script": "cpl_verify", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(rep, indent=1)
    (Path(args.out).write_text(text, encoding="utf-8") if args.out
     else print(text))
    return {"pass": 0, "violations": 1}.get(rep["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
