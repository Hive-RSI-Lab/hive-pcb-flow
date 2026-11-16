"""check_silk.py - silkscreen / assembly legibility (SPEC 6.3, P8).

One concern: silkscreen that will not assemble or read.
 - SILK-OVER-PAD: a silk item (reference designator, value, graphic) printed
   over a pad's solder-mask opening. Silkscreen ink on a pad interferes with
   solder wetting and hides the joint. Flagged when a pad's CENTRE lies under
   the silk, or the silk covers a substantial fraction of the pad - a
   graze past a pad edge (legitimate refdes tucked beside a part) is not.
 - LEGIBILITY: silk text below a minimum height / stroke thickness prints
   illegibly. (Silk LINE width vs the fab minimum is a gerber-level concern,
   left to S12 dfm_check - the independent second geometry path.)
 - ATTRIBUTION (T6): a reference designator printed so far from its own part
   that it reads as a NEIGHBOR's label - the biggest real silk defect class of
   every shipped run (EasyEDA blanket (0,-4.0) mm Reference offset; carrier:
   95 refs beyond their own pad extent, fixed by 111 move_text ops). Ported
   from the carrier P8 sweep (work/p8/silk/refdes_*.json): a visible refdes
   is silk_misattributed (warning) when it sits more than MISATTR_OWN_MM
   beyond its own footprint's pad-extent bbox AND some OTHER footprint's pad
   extent is both nearer than its own and within MISATTR_NEAR_MM - i.e. the
   text visually attaches to the wrong part ("attribution beats closeness").
   Calibrated on the corpus: flags the carrier's exact 3 shipped residuals
   and the rf4 golden's C14 (a true instance predating this check); zero on
   every other golden/mutant/shipped board. A HIDDEN refdes is never
   misattributed (it is not printed); where no spot near its own pads
   exists, place_edit set_text hides or shrinks it. Every refdes not printed
   on silk (hidden, or on a non-silk layer) is listed in the report's
   `refdes_off_silk` fact - not a violation, so the status is unchanged -
   so the assembly drawing knows which parts carry no silk label.
 - PIN LABELS (#886): a connector (J/CN/P/X ref) whose pins carry three or
   more motor-phase nets (A/B/C or U/V/W, bare or after PH/PHASE/OUT/MOTOR)
   with no silk text within LABEL_MM of its pads naming each phase is
   pin_labels_missing (warning, one per part): the cable goes on in any
   order and the motor runs backwards or not at all (minuet v3 J8, the fan
   motor's three phases, no A/B/C on the board).
 - PIN LABEL NETS (#1028): silk by a connector that names its pins names
   one wrongly - pin_label_wrong (warning). A stacked legend (one name a
   line, owned by the connector within LABEL_MM most of whose nets it
   names, judged only on a single row of pins) must name them in order down
   the row (errata corpus sizif-2: rev C's "+12 / G / G / +5" for +12V,
   VCC, +9V, GND, +5V). A one-word label within PIN_LABEL_MM of a pin
   and nearest one it doesn't name, while it names another pin of the part
   LABEL_SLACK_MM or more farther off, is the per-pin form. Names compare by _norm (3V3 = +3.3V, G = GND,
   CLK on SWCLK); unnamed nets (Net-(...)) are never named.
   A two-column legend (two words a line, class 10) by a header with two
   rows of pins must put each word over its own column, the first word on
   the reader's left; on the bottom side the reader is below, so the
   columns read mirrored from the top. Words naming pins only in the other
   column read swapped (form "columns"), or on the bottom side unmirrored
   (form "mirrored").

 - SILK POLARITY: a polarised part's silk mark on the wrong end versus its
   pads - silk_polarity_wrong (error): a diode's band or cut corners, or a
   polarised cap's "+", leaning to the A or - pad (the K or + pad by pin
   function, A/K pad names or the KiCad footprint name), or an IC's pin-1
   dot, chamfer or outline notch at a corner or end away from pin 1. The
   marks are read by cpl_verify's silk_mark, here from the board alone, with
   no CPL. A pin-1 mark is called wrong only when it is the part's one mark
   and sits by a corner pad. Parts whose mark or pad roles aren't known are
   listed in the `polarity_marks` fact as not_judged, with why.

Silk geometry is parsed here (not in geom.py, which is copper-only): top-level
gr_text / gr_line / gr_poly / gr_rect / gr_circle / gr_arc on *.SilkS, plus the
same fp_* items and the reference/value fields inside footprints, transformed to
board coordinates with the same fp_pos + R(-fp_angle).local rule geom uses for
pads. Text is approximated by its bounding rectangle; the stroke-font metrics
(char advance ~1.0 x size, box height ~1.6 x size + thickness) were calibrated
against KiCad 10's own PCB_TEXT.GetBoundingBox (see PROGRESS S5).

CLI: --pcb board.kicad_pcb [--out report.json]   exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import sexpdata
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
from checklib import violation  # noqa: E402

SCRIPT = "check_silk"
CHAR_W = 1.0          # glyph advance per char, x text size (calibrated)
TEXT_H = 1.6          # bbox height factor, x text size (calibrated)
COVER_FRAC = 0.5      # silk covering >= this fraction of a pad -> over-pad
MIN_OVERLAP_MM2 = 0.10
MIN_TEXT_H = 0.8      # legible silk text height floor (mm)
MIN_TEXT_TH = 0.12    # legible silk stroke thickness floor (mm)
MISATTR_OWN_MM = 1.0  # refdes farther than this from its own pads: suspect
MISATTR_NEAR_MM = 1.0  # ... and this close to ANOTHER part's pads: flagged
LABEL_MM = 5.0        # a pin label this near a connector's pads names them
CONNECTOR_REF = re.compile(r"^(J|CN|P|X)\d", re.I)
# a motor phase's net, by its last path segment: A, PH_B, PHASE-C, OUT_U,
# MOTOR_W ... -> the phase letter
PHASE_NET = re.compile(r"^(?:(?:PH|PHASE|OUT|MOTOR|MOT|M)[_-]?)?([ABCUVW])$",
                       re.I)


# ------------------------------------------------------------ s-expr helpers

def _tok(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _node(n):
    return isinstance(n, list) and bool(n)


def _head(n):
    return _tok(n[0]) if _node(n) else None


def _kids(n, name):
    return [c for c in n[1:] if _node(c) and _head(c) == name]


def _kid(n, name):
    for c in n[1:]:
        if _node(c) and _head(c) == name:
            return c
    return None


def _nums(n):
    return [float(x) for x in n[1:] if isinstance(x, (int, float))] \
        if _node(n) else []


def _strs(n):
    return [x for x in n[1:] if isinstance(x, str)]


def _pts(node):
    out = []
    for xy in _kids(node, "xy"):
        v = _nums(xy)
        if len(v) >= 2:
            out.append((v[0], v[1]))
    return out


def _rot(x, y, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (c * x - s * y, s * x + c * y)


def _stroke_width(node, default=0.12):
    """Line width from (stroke (width w)) or a bare (width w); default if the
    graphic carries neither (must not crash the whole check)."""
    stroke = _kid(node, "stroke")
    w = _kid(stroke, "width") if stroke is not None else _kid(node, "width")
    nums = _nums(w) if w is not None else []
    return nums[0] if nums else default


def _silk_side(layer: str):
    if layer == "F.SilkS":
        return "F"
    if layer == "B.SilkS":
        return "B"
    return None


# ------------------------------------------------------------ silk geometry

class Silk:
    __slots__ = ("side", "kind", "geom", "text", "height", "thickness", "pos",
                 "ref", "angle", "size", "just")

    def __init__(self, side, kind, g, text=None, height=0.0, thickness=0.0,
                 pos=None, ref=None, angle=0.0, size=(0.0, 0.0),
                 just=(0, 0)):
        self.side, self.kind, self.geom = side, kind, g
        self.text, self.height, self.thickness = text, height, thickness
        self.pos, self.ref, self.angle = pos, ref, angle
        self.size, self.just = size, just


def _text_box(cx, cy, angle, text, size_x, size_y, thickness,
              hjust=0, vjust=0) -> Polygon:
    """Box of a text, anchored per KiCad justification (#427): hjust -1 =
    left (box starts at the anchor), +1 = right (box ends there), 0 = centre;
    vjust -1 = top, +1 = bottom (KiCad y points down). Offsets are applied in
    the text's own frame, before rotation."""
    w = max(len(text), 1) * CHAR_W * size_x + thickness
    h = TEXT_H * size_y + thickness
    ox = -hjust * w / 2.0
    oy = -vjust * h / 2.0
    b = box(ox - w / 2.0, oy - h / 2.0, ox + w / 2.0, oy + h / 2.0)
    b = affinity.rotate(b, -angle, origin=(0, 0), use_radians=False)
    return affinity.translate(b, cx, cy)


def _is_filled(node) -> bool:
    """True when a graphic's (fill ...) says it is solid.

    KiCad writes `(fill no)` / `(fill yes)` / `(fill solid)`; an absent fill
    node means unfilled for the shapes this module draws. Only `circle` acts
    on this today - `rect`/`poly` are still buffered as solid, which is
    conservative (over-reports) rather than unsafe, but is the same latent
    false-positive class if an unfilled box ever rings a pad.
    """
    f = _kid(node, "fill")
    if f is None:
        return False
    vals = [str(t).lower() for t in (f[1:] if len(f) > 1 else [])]
    return any(v in ("yes", "true", "solid") for v in vals)


def _shape_geom(node, kind, width, fx, fy, fangle):
    """shapely geometry for a gr_/fp_ graphic in board coords."""
    def xf(p):
        dx, dy = _rot(p[0], p[1], -fangle)
        return (fx + dx, fy + dy)

    w2 = max(width, 0.05) / 2.0
    if kind.endswith("line"):
        s, e = _kid(node, "start"), _kid(node, "end")
        if s is None or e is None:
            return None
        return LineString([xf(_nums(s)[:2]), xf(_nums(e)[:2])]).buffer(w2)
    if kind.endswith("rect"):
        s, e = _nums(_kid(node, "start")), _nums(_kid(node, "end"))
        if len(s) < 2 or len(e) < 2:
            return None
        pts = [xf((s[0], s[1])), xf((e[0], s[1])), xf((e[0], e[1])), xf((s[0], e[1]))]
        return Polygon(pts).buffer(w2)
    if kind.endswith("circle"):
        c, e = _nums(_kid(node, "center")), _nums(_kid(node, "end"))
        if len(c) < 2 or len(e) < 2:
            return None
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        ctr = Point(xf((c[0], c[1])))
        # An UNFILLED circle is an annulus, not a disc. Building it as a disc
        # made every stock ring footprint - TestPoint_Pad_*, fiducials,
        # polarity rings - report its own pad as 100% silk-covered: bb-buck P8
        # got "silk circle covers pad TP1.1 (1.77 mm2)", and 1.77 = pi*0.75^2 is
        # exactly the pad, the signature of the disc assumption. Real geometry
        # there: ring r 0.89-1.01 mm vs a 0.75 mm pad, i.e. 0.14 mm of CLEARANCE.
        if not _is_filled(node):
            inner = r - w2
            if inner > 0:
                return ctr.buffer(r + w2).difference(ctr.buffer(inner))
        return ctr.buffer(r + w2)
    if kind.endswith("poly"):
        pn = _kid(node, "pts")
        pts = _pts(pn) if pn is not None else _pts(node)
        if len(pts) >= 3:
            return Polygon([xf(p) for p in pts]).buffer(w2)
        if len(pts) == 2:
            return LineString([xf(pts[0]), xf(pts[1])]).buffer(w2)
    if kind.endswith("arc"):
        s, m, e = _kid(node, "start"), _kid(node, "mid"), _kid(node, "end")
        if s and m and e:
            pts = geom._arc_points(tuple(_nums(s)[:2]), tuple(_nums(m)[:2]),
                                   tuple(_nums(e)[:2]))
            return LineString([xf(p) for p in pts]).buffer(w2)
    return None


def _hidden(node) -> bool:
    # KiCad 6 writes a bare `hide` atom at the end of an fp_text (#1429)
    if any(isinstance(x, sexpdata.Symbol) and x.value() == "hide"
           for x in node[1:]):
        return True
    h = _kid(node, "hide")
    if h is not None:
        return not _nums(h) and "no" not in [_tok(x) for x in h[1:]]
    return False


def _justify(eff) -> tuple[int, int]:
    """(hjust, vjust) from an (effects (justify ...)) node: -1/0/+1 each.
    `mirror` (bottom-side text) draws the glyphs flipped, so a left-justified
    mirrored text runs to the LEFT of its anchor in board coordinates."""
    j = _kid(eff, "justify") if eff is not None else None
    if j is None:
        return 0, 0
    toks = {str(_tok(t)).lower() for t in j[1:]}
    hj = -1 if "left" in toks else (1 if "right" in toks else 0)
    vj = -1 if "top" in toks else (1 if "bottom" in toks else 0)
    if "mirror" in toks:
        hj = -hj
    return hj, vj


def _text_item(node, fx, fy, fangle, side, text) -> Silk | None:
    if _hidden(node) or not (text or "").strip():
        return None
    at = _kid(node, "at")
    if at is None:
        return None
    a = _nums(at)
    lx, ly = a[0], a[1]
    langle = a[2] if len(a) > 2 else 0.0
    dx, dy = _rot(lx, ly, -fangle)
    cx, cy = fx + dx, fy + dy
    # position is local (transform by the footprint); the stored text angle is
    # already ABSOLUTE board-frame, exactly like pad angles (LEARNINGS
    # [geometry]) - do NOT add the footprint rotation.
    angle = langle
    eff = _kid(node, "effects")
    font = _kid(eff, "font") if eff is not None else None
    size = _kid(font, "size") if font is not None else None
    sz = _nums(size) if size is not None else [1.0, 1.0]
    sx, sy = (sz[0], sz[1]) if len(sz) >= 2 else (1.0, 1.0)
    th_node = _kid(font, "thickness") if font is not None else None
    th = _nums(th_node)[0] if th_node is not None and _nums(th_node) else 0.15
    hj, vj = _justify(eff)
    g = _text_box(cx, cy, angle, text, sx, sy, th, hj, vj)
    return Silk(side, "text", g, text=text, height=sy, thickness=th,
                pos=(cx, cy), angle=angle, size=(sx, sy), just=(hj, vj))


def parse_silk(root) -> list[Silk]:
    items: list[Silk] = []
    # top-level graphics
    for kind in ("gr_text", "gr_line", "gr_rect", "gr_circle", "gr_poly", "gr_arc"):
        for node in _kids(root, kind):
            ln = _kid(node, "layer")
            side = _silk_side(_strs(ln)[0]) if ln is not None and _strs(ln) else None
            if side is None:
                continue
            if kind == "gr_text":
                it = _text_item(node, 0.0, 0.0, 0.0, side,
                                _strs(node)[0] if _strs(node) else "")
                if it is not None:
                    items.append(it)
            else:
                w = _stroke_width(node)
                g = _shape_geom(node, kind, w, 0.0, 0.0, 0.0)
                if g is not None and not g.is_empty:
                    items.append(Silk(side, kind.replace("gr_", ""), g,
                                      thickness=w))
    # footprint graphics + reference/value fields
    for fp in _kids(root, "footprint"):
        fat = _kid(fp, "at")
        fn = _nums(fat) if fat is not None else [0, 0, 0]
        fx, fy = fn[0], fn[1]
        fangle = fn[2] if len(fn) > 2 else 0.0
        first = len(items)
        # reference/value/user fields are stored as (property "Name" "Value" ...)
        for prop in _kids(fp, "property"):
            ln = _kid(prop, "layer")
            side = _silk_side(_strs(ln)[0]) if ln is not None and _strs(ln) else None
            pv = _strs(prop)
            if side is None or len(pv) < 2:
                continue
            it = _text_item(prop, fx, fy, fangle, side, pv[1])
            if it is not None:
                items.append(it)
        for kind in ("fp_text", "fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc"):
            for node in _kids(fp, kind):
                ln = _kid(node, "layer")
                side = _silk_side(_strs(ln)[0]) if ln is not None and _strs(ln) else None
                if side is None:
                    continue
                if kind == "fp_text":
                    # (fp_text KIND "TEXT" ...): KiCad 6 may end it with
                    # a bare `hide`, so the text is the second atom, not
                    # the last
                    st = _strs(node)
                    it = _text_item(node, fx, fy, fangle, side,
                                    st[1] if len(st) > 1 else "")
                    if it is not None:
                        items.append(it)
                else:
                    w = _stroke_width(node)
                    g = _shape_geom(node, kind, w, fx, fy, fangle)
                    if g is not None and not g.is_empty:
                        items.append(Silk(side, kind.replace("fp_", ""), g,
                                          thickness=w))
        fref = None      # KiCad 7+: a Reference property; KiCad 6: fp_text
        for prop in _kids(fp, "property"):
            pv = _strs(prop)
            if len(pv) >= 2 and pv[0] == "Reference":
                fref = pv[1]
        for node in _kids(fp, "fp_text"):
            tv = _strs(node)
            if fref is None and len(node) > 2 and _tok(node[1]) == "reference" \
                    and tv:
                fref = tv[-1]
        for it in items[first:]:
            it.ref = fref
    return items


# ------------------------------------------------------ refdes attribution

def refdes_texts(root) -> list[tuple[str, "Silk"]]:
    """(refdes, Silk) for every visible Reference field on a silk layer."""
    out: list[tuple[str, Silk]] = []
    for fp in _kids(root, "footprint"):
        fat = _kid(fp, "at")
        fn = _nums(fat) if fat is not None else [0, 0, 0]
        fx, fy = fn[0], fn[1]
        fangle = fn[2] if len(fn) > 2 else 0.0
        for prop in _kids(fp, "property"):
            pv = _strs(prop)
            if len(pv) < 2 or pv[0] != "Reference":
                continue
            ln = _kid(prop, "layer")
            strs = _strs(ln) if ln is not None else []
            side = _silk_side(strs[0]) if strs else None
            if side is None:
                continue
            it = _text_item(prop, fx, fy, fangle, side, pv[1])
            if it is not None:
                out.append((pv[1], it))
    return out


def refdes_off_silk(root) -> list[dict]:
    """[{ref, why}] for every footprint whose Reference is not printed on
    silk: hidden (place_edit set_text hide) or on a non-silk layer. Board-only
    parts (mounting holes, logos) are listed too - the list is for the
    assembly drawing, which wants every unlabelled part."""
    out = []
    for fp in _kids(root, "footprint"):
        for prop in _kids(fp, "property"):
            pv = _strs(prop)
            if len(pv) < 2 or pv[0] != "Reference" or not pv[1].strip():
                continue
            ln = _kid(prop, "layer")
            strs = _strs(ln) if ln is not None else []
            layer = strs[0] if strs else None
            if _hidden(prop):
                out.append({"ref": pv[1], "why": "hidden"})
            elif _silk_side(layer or "") is None:
                out.append({"ref": pv[1], "why": f"on {layer}"})
    return sorted(out, key=lambda d: d["ref"])


def text_geom(text: str, x: float, y: float, angle: float, size_x: float,
              size_y: float, thickness: float) -> Polygon:
    """The text box every check_silk rule measures (board coords; angle is
    ABSOLUTE). silk_place builds its candidates' rule geometry here, so the
    solver and this checker never measure a label two ways."""
    return _text_box(x, y, angle, text, size_x, size_y, thickness)


def pad_extent_boxes(bg: geom.BoardGeom) -> dict[str, Polygon]:
    """{ref: bbox of that footprint's pad extent} - the 'own pads' and
    'other part' shapes of the attribution rule."""
    extent: dict[str, list[float]] = {}
    for p in bg.pads_of():
        b = p.poly.bounds
        e = extent.get(p.ref)
        if e is None:
            extent[p.ref] = list(b)
        else:
            e[0] = min(e[0], b[0]); e[1] = min(e[1], b[1])
            e[2] = max(e[2], b[2]); e[3] = max(e[3], b[3])
    return {r: box(*b) for r, b in extent.items()}


def attribution(ref: str, g, boxes: dict[str, Polygon]):
    """The ONE misattribution rule: check_attribution here, and candidate
    acceptance in silk_place. -> (own_off, nearest_ref, nearest_d, flagged),
    or None for a padless footprint (logo, graphic).

    nearest_* is the closest OTHER part's pad extent no farther than own_off
    (None when no other part is that close); a part farther away than the
    label's own pads can neither flag it nor out-rank its own part. flagged:
    the label sits more than MISATTR_OWN_MM beyond its own pads AND another
    part is nearer than min(MISATTR_NEAR_MM, own_off)."""
    own = boxes.get(ref)
    if own is None:
        return None
    own_off = g.distance(own)
    gx0, gy0, gx1, gy1 = g.bounds
    nearest_ref, nearest_d = None, None
    for other, ob in boxes.items():
        if other == ref:
            continue
        ox0, oy0, ox1, oy1 = ob.bounds
        if ox0 > gx1 + own_off or ox1 < gx0 - own_off \
                or oy0 > gy1 + own_off or oy1 < gy0 - own_off:
            continue                     # bounds farther than own pads
        d = g.distance(ob)
        if d <= own_off and (nearest_d is None or d < nearest_d):
            nearest_ref, nearest_d = other, d
    flagged = (own_off > MISATTR_OWN_MM and nearest_d is not None
               and nearest_d < min(MISATTR_NEAR_MM, own_off))
    return own_off, nearest_ref, nearest_d, flagged


def check_attribution(bg: geom.BoardGeom, root) -> list[dict]:
    """silk_misattributed: refdes text that reads against a neighbor (see
    module docstring). Distances are text-bbox to pad-extent bbox per ref."""
    boxes = pad_extent_boxes(bg)
    violations: list[dict] = []
    for ref, s in refdes_texts(root):
        a = attribution(ref, s.geom, boxes)
        if a is None or not a[3]:
            continue
        own_off, nearest_ref, nearest_d, _ = a
        violations.append(violation(
            SCRIPT, "warning", s.pos, f"{s.side}.SilkS", None, [ref],
            f'refdes "{ref}" sits {own_off:.2f} mm beyond its own pads and '
            f"{nearest_d:.2f} mm from {nearest_ref} - reads as {nearest_ref}'s "
            "label; scripted fix: place_edit.py move_text, or set_text "
            "(hide / shrink) when no spot near its own pads passes", SCRIPT,
            kind="silk_misattributed", ref=ref,
            offset_mm=checklib.rnd(own_off), nearest_ref=nearest_ref,
            nearest_mm=checklib.rnd(nearest_d)))
    return violations


# ------------------------------------------------------------ checks

def pad_side(pad) -> set[str]:
    out = set()
    if "F.Cu" in pad.layers:
        out.add("F")
    if "B.Cu" in pad.layers:
        out.add("B")
    return out


def over_pad(silk, pad) -> tuple[bool, float]:
    """(is_over, overlap_area). Over if the pad centre is under the silk, or the
    silk covers a substantial fraction of the pad (not a mere edge graze).
    `silk` is a Silk or a bare geometry: silk_place tests its candidates with
    this same rule."""
    g = silk.geom if isinstance(silk, Silk) else silk
    pp = pad.poly
    if not g.intersects(pp):
        return False, 0.0
    inter = g.intersection(pp)
    area = inter.area
    center_in = g.covers(Point(pad.center))
    substantial = area >= MIN_OVERLAP_MM2 and area >= COVER_FRAC * pp.area
    return (center_in or substantial), area


def run_checks(bg: geom.BoardGeom, silks: list[Silk]):
    violations: list[dict] = []
    pads = [p for p in bg.pads_of() if p.layers]
    for s in silks:
        for pad in pads:
            if s.side not in pad_side(pad):
                continue
            hit, area = over_pad(s, pad)
            if not hit:
                continue
            label = f'"{s.text}"' if s.text else f"{s.kind}"
            violations.append(violation(
                SCRIPT, "error", pad.center, f"{s.side}.SilkS", pad.net,
                [pad.ref], f"silk {label} on {s.side}.SilkS covers pad "
                f"{pad.ref}.{pad.number} ({area:.2f} mm2)", SCRIPT,
                kind="silk_over_pad", ref=pad.ref, pad=pad.number,
                overlap_mm2=checklib.rnd(area), silk_kind=s.kind,
                silk_text=s.text))
    # legibility
    for s in silks:
        if s.kind != "text":
            continue
        if s.height + 1e-6 < MIN_TEXT_H:
            violations.append(violation(
                SCRIPT, "warning", s.pos, f"{s.side}.SilkS", None, [],
                f'silk text "{s.text}" is {s.height:.2f} mm tall '
                f"(< {MIN_TEXT_H} mm min legible height)", SCRIPT,
                kind="silk_illegible", silk_text=s.text,
                height_mm=checklib.rnd(s.height)))
        elif s.thickness + 1e-6 < MIN_TEXT_TH:
            violations.append(violation(
                SCRIPT, "warning", s.pos, f"{s.side}.SilkS", None, [],
                f'silk text "{s.text}" stroke {s.thickness:.3f} mm '
                f"(< {MIN_TEXT_TH} mm min)", SCRIPT, kind="silk_thin",
                silk_text=s.text, thickness_mm=checklib.rnd(s.thickness)))
    return violations


def _phase(net: str | None) -> str | None:
    m = PHASE_NET.match((net or "").rsplit("/", 1)[-1])
    return m.group(1).upper() if m else None


def check_pin_labels(bg: geom.BoardGeom, silks: list[Silk]) -> list[dict]:
    """pin_labels_missing: a multi-phase connector with no phase names on
    silk near its pads (see module docstring)."""
    out = []
    texts = [s for s in silks if s.kind == "text" and s.text]
    for ref in sorted({p.ref for p in bg.pads_of()
                       if p.ref and CONNECTOR_REF.match(p.ref)}):
        pads = bg.pads_of(ref=ref)
        phases = {_phase(p.net): p.net for p in pads if _phase(p.net)}
        if len(phases) < 3:
            continue
        ext = unary_union([p.poly for p in pads])
        tokens = set()
        for s in texts:
            if s.geom.distance(ext) <= LABEL_MM:
                tokens.update(t.upper() for t in re.split(r"[^A-Za-z0-9]+",
                                                          s.text) if t)
        want = sorted(phases)
        # each phase named on its own (A B C), or run together (ABC)
        if all(ph in tokens for ph in want) or any(
                len(t) == len(want) and sorted(t) == want for t in tokens):
            continue
        c = ext.centroid
        nets = [phases[ph] for ph in want]
        out.append(violation(
            SCRIPT, "warning", (c.x, c.y), None, None, [ref],
            f"{ref} carries {len(want)} motor phases ({', '.join(nets)}) but "
            f"no silk text within {LABEL_MM} mm names its pins "
            f"({'/'.join(want)}): the cable can go on in any order. Print "
            "the phase names beside the pins", SCRIPT,
            kind="pin_labels_missing", phases=want, nets=nets))
    return out


# ------------------------------------------------------ pin labels vs nets

GND_NAMES = {"G", "GND", "GROUND", "0V", "VSS"}
LABEL_SLACK_MM = 0.5  # a label this much nearer another pin is that pin's
PIN_LABEL_MM = 1.5    # a one-word pin label sits this close to its pin's pad
ROW_ACROSS_MM = 1.0   # pads within this of one line (across it) are one row


NEG_RAIL = re.compile(r"(?:(?:-|MIN|NEG)(?=\d+(?:\.\d+)?V?(?:\d|_|$))"
                      r"|M(?=\d+(?:\.\d+)?V(?:\d|_|$)))")  # M1 is a motor


def _norm(name: str | None) -> str:
    """A net name or a silk word in one spelling: the last path part, upper
    case, no leading +, no ~{} or overbar marks, GND for any ground word, and
    a voltage as N.NV (3V3, 3.3, +3.3V -> 3.3V; 12, +12V -> 12V). A negative
    rail keeps its sign as a leading - whichever way it is spelt (#1456):
    -5V, MIN5V, NEG5V and M5V are all -5V, so -5V_DIV is MIN5V_DIV."""
    n = (name or "").rsplit("/", 1)[-1].strip().upper()
    n = re.sub(r"[~{}]", "", n).lstrip("+")
    m = NEG_RAIL.match(n)
    if m:
        return "-" + _norm(n[m.end():])
    if n in GND_NAMES:
        return "GND"
    m = re.fullmatch(r"(\d+)V(\d+)", n)
    if m:
        return f"{m.group(1)}.{m.group(2)}V"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)V?", n)
    if m:
        return f"{m.group(1)}V"
    return n


def _named(net: str | None) -> bool:
    n = (net or "").rsplit("/", 1)[-1]
    return bool(n) and not re.match(r"(?i)(Net-\(|unconnected-)", n)


def _names(token: str, net: str | None) -> bool:
    """Does a silk word name this net: the same, or one ends with the other
    (CLK on SWCLK, at 3 letters or more)."""
    if not _named(net):
        return False
    t, n = _norm(token), _norm(net)
    if not t:
        return False
    if t == n:
        return True
    short, long_ = sorted((t, n), key=len)
    if long_ == "-" + short:
        return False                         # 5V_DIV is not -5V_DIV (#1456)
    return len(short) >= 3 and long_.endswith(short)


LINE_PITCH = 1.62     # KiCad's interline, x text height


def _block(s) -> Polygon:
    """The box of a text over all its lines (Silk.geom is one line's, as
    wide as the whole string): lines LINE_PITCH x height apart, the block
    justified on the anchor as KiCad does."""
    lines = s.text.split("\n")
    if len(lines) < 2:
        return s.geom
    sx, sy = s.size if s.size[1] else (1.0, 1.0)
    hj, vj = s.just
    w = max(len(ln) for ln in lines) * CHAR_W * sx + s.thickness
    h = (len(lines) - 1) * LINE_PITCH * sy + TEXT_H * sy + s.thickness
    ox, oy = -hj * w / 2.0, -vj * h / 2.0
    b = box(ox - w / 2.0, oy - h / 2.0, ox + w / 2.0, oy + h / 2.0)
    b = affinity.rotate(b, -(s.angle or 0.0), origin=(0, 0), use_radians=False)
    return affinity.translate(b, s.pos[0], s.pos[1])


def _label_owner(g, conns: dict, all_pads) -> str | None:
    """The connector a silk text labels: the part whose pad is nearest it,
    if that is a connector and within LABEL_MM."""
    near = min(all_pads, key=lambda p: g.distance(p.poly), default=None)
    if near is None or near.ref not in conns:
        return None
    return near.ref if g.distance(near.poly) <= LABEL_MM else None


def _stack_owner(g, conns: dict, lines: list[str]) -> str | None:
    """The connector a stacked label lists: a legend often sits past the
    end of the row, nearer some other part's pad, so of the connectors
    within LABEL_MM take the one most of whose nets the lines name (ties to
    the nearer)."""
    best, key = None, None
    for ref, pads in conns.items():
        d = min(g.distance(p.poly) for p in pads)
        if d > LABEL_MM:
            continue
        n = sum(1 for ln in lines if ln and any(_names(ln, p.net) for p in pads))
        k = (n, -d)
        if n >= 2 and (key is None or k > key):
            best, key = ref, k
    return best


def check_label_nets(bg: geom.BoardGeom, silks: list[Silk]) -> list[dict]:
    """pin_label_wrong (#1028, errata corpus sizif-2): silk beside a
    connector names its pins, and a name sits on the wrong pin. Two forms:
    a stacked label (one name per line, read down the text) whose names
    don't follow the pins in order down the row, and a one-word label
    nearest a pin whose net it doesn't name while another pin's net is the
    one it names. A label is judged only when it reads as one: at least two
    of a stack's lines, and half of them, name a net of the connector."""
    out = []
    all_pads = [p for p in bg.pads_of() if p.ref]
    conns = {}
    for p in all_pads:
        if CONNECTOR_REF.match(p.ref):
            conns.setdefault(p.ref, []).append(p)
    refs = {p.ref for p in all_pads}
    texts = [s for s in silks if s.kind == "text" and s.text
             and s.text.strip() not in refs]
    for s in texts:
        g = _block(s)
        lines = [ln.strip() for ln in s.text.split("\n")]
        pairs = [ln.split() for ln in lines if ln]
        if len(pairs) >= 2 and all(len(t) == 2 for t in pairs):
            v = _columns(s, g, conns, pairs)
            if v:
                out.append(v)
            continue
        ref = (_stack_owner(g, conns, lines) if len(lines) >= 2
               else _label_owner(g, conns, all_pads))
        if ref is None:
            continue
        pads = conns[ref]
        if len(lines) >= 2:
            v = _stack(s, ref, pads, lines)
        else:
            v = _single(s, ref, pads, lines[0])
        if v:
            out.append(v)
    return out


def _stack(s, ref, pads, lines):
    a = math.radians(s.angle or 0.0)
    down = (math.sin(a), math.cos(a))      # KiCad: y down, angle CCW
    along = lambda p: p.center[0] * down[0] + p.center[1] * down[1]  # noqa: E731
    across = [p.center[0] * down[1] - p.center[1] * down[0] for p in pads]
    if len(pads) < 2 or max(across) - min(across) > ROW_ACROSS_MM:
        return None                          # not one row down the text
    row = sorted(pads, key=along)
    cand = [[i for i, p in enumerate(row) if _names(ln, p.net)]
            if ln and "..." not in ln else [] for ln in lines]
    named = [c for c in cand if c]
    if len(named) < 2 or 2 * len(named) < sum(1 for ln in lines if ln):
        return None
    last, bad = -1, None
    for ln, c in zip(lines, cand):
        if not c:
            continue
        nxt = [i for i in c if i > last]
        if not nxt:
            bad = ln
            break
        last = nxt[0]
    if bad is None:
        return None
    pins = ", ".join(f"{p.number} {(p.net or '').rsplit('/', 1)[-1]}"
                     for p in row)
    label = " / ".join(ln for ln in lines if ln)
    return violation(
        SCRIPT, "warning", s.pos, f"{s.side}.SilkS", None, [ref],
        f"silk label \"{label}\" beside {ref} names its pins out of order: "
        f"down the row they are {pins}, and \"{bad}\" has no pin left to "
        "name below the ones before it. A cable made to the silk goes on "
        "wrong", SCRIPT, kind="pin_label_wrong", silk_text=s.text,
        form="stack", lines=len([ln for ln in lines if ln]), pins=len(row))


def _columns(s, g, conns, pairs):
    """Class 10: a two-column legend, "SDA SCL / 3V3 GND", read line by
    line against a header's two rows of pins. Each word should name a pin
    in its own column: the first word the column on the reader's left, the
    second the right one. Seen from the top, bottom-side text is mirrored,
    so its reader's left is the top view's right. A legend whose words name
    pins only in the other column (two at least, none in their own) reads
    swapped, or on the bottom side was laid out for the top and never
    mirrored. Words naming a pin in both columns (GND twice) don't count."""
    best, key = None, None
    words = [w for t in pairs for w in t]
    for ref, pads in conns.items():
        d = min(g.distance(p.poly) for p in pads)
        if d > LABEL_MM:
            continue
        n = sum(1 for w in words if any(_names(w, p.net) for p in pads))
        if n >= 2 and 2 * n >= len(words) and (key is None or (n, -d) > key):
            best, key = ref, (n, -d)
    if best is None:
        return None
    pads = conns[best]
    a = math.radians(s.angle or 0.0)
    right = (math.cos(a), -math.sin(a))     # reading direction, top view
    if s.side == "B":
        right = (-right[0], -right[1])      # mirrored: read from below
    pos = lambda p: p.center[0] * right[0] + p.center[1] * right[1]  # noqa: E731
    across = sorted(pos(p) for p in pads)
    gaps = [(across[i + 1] - across[i], i) for i in range(len(across) - 1)]
    if not gaps:
        return None
    gap, i = max(gaps)
    if (gap <= ROW_ACROSS_MM or across[i] - across[0] > ROW_ACROSS_MM
            or across[-1] - across[i + 1] > ROW_ACROSS_MM):
        return None                          # not two rows of pins
    cut = (across[i] + across[i + 1]) / 2.0
    col = ([p for p in pads if pos(p) < cut], [p for p in pads if pos(p) >= cut])
    own = other = 0
    for t in pairs:
        for k, w in enumerate(t):
            here = any(_names(w, p.net) for p in col[k])
            there = any(_names(w, p.net) for p in col[1 - k])
            own += here and not there
            other += there and not here
    if other < 2 or own:
        return None
    label = " / ".join(" ".join(t) for t in pairs)
    left = ", ".join(sorted({(p.net or "").rsplit("/", 1)[-1] for p in col[0]
                             if _named(p.net)}))
    if s.side == "B":
        form, why = "mirrored", (
            "it is on the bottom side and reads in the top side's column "
            "order: from below, the pins on the reader's left are " + left)
    else:
        form, why = "columns", (
            "its two columns are swapped against the pads: the pins on the "
            "reader's left are " + left)
    return violation(
        SCRIPT, "warning", s.pos, f"{s.side}.SilkS", None, [best],
        f"silk legend \"{label}\" beside {best}: {why}. A cable made to the "
        "silk goes on wrong", SCRIPT, kind="pin_label_wrong", silk_text=s.text,
        form=form, lines=len(pairs), pins=len(pads))


def _pin_end(s, pads) -> Point:
    """Where a one-word label points at its pin: the middle of its box (the
    anchor can be either end, by justification), but for slanted text the
    end of the word nearer the pads. A label at 30 degrees leaves its pin
    and runs on over the next one, so its middle sits by the wrong pin
    (#1456, zxdiag-v10 J202)."""
    c = s.geom.centroid
    a = (s.angle or 0.0) % 90.0
    if a < 1.0 or a > 89.0:
        return c
    sx = s.size[0] or 1.0
    half = (len(s.text.strip()) * CHAR_W * sx + s.thickness) / 2.0
    r = math.radians(s.angle)
    dx, dy = half * math.cos(r), -half * math.sin(r)
    ends = [Point(c.x - dx, c.y - dy), Point(c.x + dx, c.y + dy)]
    return min(ends, key=lambda e: min(e.distance(p.poly) for p in pads))


def _single(s, ref, pads, word):
    if not word or " " in word:
        return None
    hits = [p for p in pads if _names(word, p.net)]
    if not hits:
        return None
    pt = _pin_end(s, pads)
    near = min(pads, key=lambda p: pt.distance(Point(p.center)))
    if near in hits or _names(word, near.net):
        return None
    if s.geom.distance(near.poly) > PIN_LABEL_MM:
        return None                          # not beside a pin: some other note
    best = min(hits, key=lambda p: pt.distance(Point(p.center)))
    gap = pt.distance(Point(best.center)) - pt.distance(Point(near.center))
    if gap < LABEL_SLACK_MM:
        return None
    return violation(
        SCRIPT, "warning", s.pos, f"{s.side}.SilkS", near.net, [ref],
        f"silk label \"{word}\" sits by {ref} pin {near.number} "
        f"({near.net}) but names pin {best.number}'s net ({best.net}), "
        f"{gap:.1f} mm farther away", SCRIPT, kind="pin_label_wrong",
        silk_text=s.text, form="pin", pin=near.number, named_pin=best.number)


# ------------------------------------------------------- silk polarity marks

NOTCH_CHORD_MM = (0.4, 3.0)  # an outline notch's width
NOTCH_SAG = 0.25             # its depth, as a share of its width at least
NOTCH_ACROSS = 0.25          # how far off the part's middle line it may sit


def _notch(cv, fp):
    """(where, "ok"|"wrong") for a two-row part's outline notch: an open arc
    on its own silk, beyond the end pads along the rows and about on the
    middle line between them. Pin 1's end of the rows is where it belongs.
    None where there is no single notch, or the part is not in two rows."""
    pads = [p for p in fp.pads if p.number]
    pin = next((p for p in pads if p.number == "1"), None)
    if pin is None:
        return None
    xs, ys = {round(p.x, 2) for p in pads}, {round(p.y, 2) for p in pads}
    if len(xs) == 2 and len(ys) > 2:
        axis = 1
    elif len(ys) == 2 and len(xs) > 2:
        axis = 0
    else:
        return None
    box = cv._pad_field(fp)
    lo, hi = box[axis], box[axis + 2]
    a0, a1 = box[1 - axis], box[3 - axis]
    mid_v, mid_u, width = (lo + hi) / 2, (a0 + a1) / 2, a1 - a0
    ends = set()
    where = None
    for s in fp.own_silk:
        if len(s) != 3 or math.dist(s[0], s[2]) <= cv.ARC_CLOSED_MM:
            continue
        a, m, e = s
        chord = math.dist(a, e)
        c = ((a[0] + e[0]) / 2, (a[1] + e[1]) / 2)
        if not (NOTCH_CHORD_MM[0] <= chord <= NOTCH_CHORD_MM[1]
                and math.dist(c, m) >= NOTCH_SAG * chord
                and abs(c[1 - axis] - mid_u) <= NOTCH_ACROSS * width
                and not lo < c[axis] < hi):
            continue
        ends.add(c[axis] > mid_v)
        where = (round(c[0], 3), round(c[1], 3))
    pin_v = (pin.x, pin.y)[axis]
    if len(ends) != 1 or abs(pin_v - mid_v) < 1e-6:
        return None
    return where, ("ok" if ends == {pin_v > mid_v} else "wrong")


KICAD5_MODULE = re.compile(r"^\s*\(module\s", re.M)
FIGURE_STROKES = 7  # a seven-segment display's 8 is seven strokes


def _strokes_inside(cv, fp, board: bool) -> int:
    """Silk line strokes wholly inside the pad field: a figure drawn on the
    part's body (a display's segments), where a dot is its decimal point."""
    x0, y0, x1, y1 = cv._pad_field(fp)
    silk = fp.board_silk if board else fp.own_silk
    return sum(1 for s in silk if len(s) == 2 and all(
        x0 < x < x1 and y0 < y < y1 for x, y in s))


def _corner_pads(cv, fp) -> set[str]:
    """The numbered pads nearest each corner of the pad field."""
    pads = [p for p in fp.pads if p.number]
    x0, y0, x1, y1 = cv._pad_field(fp)
    return {min(pads, key=lambda p: math.hypot(p.x - cx, p.y - cy)).number
            for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))}


KICAD_POLAR_NAME = re.compile(r"^(D_|LED_|CP_|C_Elec)")


def _roles_guessed(cv, fp) -> bool:
    """True where board_roles' K/A comes only from its last resort: an
    imported "LED..." footprint read with KiCad's numbering (pin 1 = K). No
    pin function, no A/K pad names, no KiCad library name: an imported
    library may number it the other way (hifi-receiver-2's LCSC
    LED-SMD_L1.6-W0.8-R-RD has its anode net on pad 1 and the cut corner by
    pad 2, on GND), so a band read against that guess says nothing."""
    if any(cv.role(p.function) for p in fp.pads):
        return False
    named = {p.number.upper() for p in fp.pads}
    if named in ({"A", "C"}, {"A", "K"}):
        return False
    return not KICAD_POLAR_NAME.match(fp.name)


def polarity_mark_of(cv, fp) -> dict:
    """{"state": "ok"|"wrong"|"not_judged"|None, "why", ...} for one part's
    silk polarity mark against its pads, from the board alone. cpl_verify's
    reader finds the mark (silk_mark: a two-pad part's band, cut corners or
    "+" against its K or + pad; a U/IC/Q part's or ball grid's dot, small
    shape or chamfer against pin 1's corner); a two-row IC with no dot may
    show a notch instead. A pin-1 mark is only called wrong when it is the
    part's one mark and sits by a corner pad: a mark by a pad mid-row (a
    display's decimal point, a library's every-tenth-pin tick) or one of
    several marks says nothing sure about pin 1. None: not a polarised part."""
    m = cv.silk_mark(fp)
    st = m["state"]
    nums = {p.number for p in fp.pads if p.number}
    if m.get("basis") == "polarity" and _roles_guessed(cv, fp):
        return {"state": "not_judged",
                "why": "which pad is the cathode is only guessed from an "
                       "imported LED footprint's name (no pin function or "
                       "A/K pad names); its library may number it the "
                       "other way"}
    if st is None:
        if (len(nums) != 2 or not cv.is_polar(fp) or cv.is_keyed(fp)
                or cv.is_switch(fp)):
            return {"state": None}
        roles = cv.board_roles(fp, None)
        if not ({"K", "A"} <= set(roles.values())
                or {"+", "-"} <= set(roles.values())):
            return {"state": "not_judged",
                    "why": "which pad is the cathode or + is not known (no "
                           "pin function on its pads, no A/K pad names, and "
                           "its footprint name does not say)"}
        return {"state": "not_judged",
                "why": "no band, cut corner or \"+\" on its silk that leans "
                       "to one pad"}
    if st == "none":
        if m.get("basis") != "polarity":
            n = _notch(cv, fp)
            if n:
                where, verdict = n
                return {"state": verdict, "pin": "1", "form": "notch",
                        "at": where,
                        "why": f"its outline notch at {where} is at the end "
                               f"of the rows away from pin 1"}
        return {"state": "not_judged",
                "why": "no polarity or pin-1 mark found on its silk"}
    if st == "ok":
        return {"state": "ok", "pin": m.get("pin")}
    if m.get("basis") == "polarity":
        return {"state": "wrong", "pin": m.get("pin"), "form": "band",
                "at": (m.get("off") or [None])[0], "why": m.get("why")}
    marks = (fp.board_marks if m.get("source") == "board" else fp.marks)
    if len(marks) != 1:
        return {"state": "not_judged",
                "why": f"{len(marks)} mark-like shapes on its silk, so which "
                       f"one is pin 1's is not sure"}
    mx, my = marks[0]
    inside = _strokes_inside(cv, fp, m.get("source") == "board")
    if inside >= FIGURE_STROKES:
        return {"state": "not_judged",
                "why": f"its mark sits in a figure drawn inside its pads "
                       f"({inside} strokes: a display's segments and "
                       f"decimal point), not read as a pin-1 mark"}
    pads = [p for p in fp.pads if p.number]
    near = min(pads, key=lambda p: math.hypot(p.x - mx, p.y - my))
    if near.number not in _corner_pads(cv, fp):
        return {"state": "not_judged",
                "why": f"its one mark sits by pad {near.number}, mid-row, "
                       f"so it is not read as a pin-1 mark"}
    return {"state": "wrong", "pin": m.get("pin"), "form": "dot",
            "at": (round(mx, 3), round(my, 3)),
            "why": f"its pin-1 mark sits by pad {near.number}'s corner, not "
                   f"pin {m.get('pin')}'s"}


def check_polarity_marks(pcb) -> tuple[list[dict], dict]:
    """SILK POLARITY: every polarised part's silk mark against its pads,
    from the board alone (no CPL). Returns the violations and a fact:
    counts of ok / wrong / not_judged, and the not_judged refs with why."""
    import cpl_verify as cv
    vios, ok, unjudged = [], 0, {}
    fps = cv.board_footprints(pcb)
    if not fps:
        old = len(KICAD5_MODULE.findall(Path(pcb).read_text(encoding="utf-8")))
        if old:
            return [], {"ok": 0, "wrong": 0, "not_judged": old,
                        "not_judged_refs": [], "not_judged_board": (
                            f"a KiCad 5 board: its {old} parts are `module` "
                            f"blocks, which the part reader does not read")}
    for ref in sorted(fps, key=cv._natural):
        fp = fps[ref]
        r = polarity_mark_of(cv, fp)
        if r["state"] == "ok":
            ok += 1
        elif r["state"] == "not_judged":
            unjudged.setdefault(r["why"], []).append(ref)
        elif r["state"] == "wrong":
            layer = ("F" if fp.side == "top" else "B") + ".SilkS"
            vios.append(violation(
                SCRIPT, "error", (fp.x, fp.y), layer, None, [ref],
                f"{ref} ({fp.name}): the silk polarity mark is on the wrong "
                f"end: {r['why']}", SCRIPT, kind="silk_polarity_wrong",
                ref=ref, footprint=fp.name, mark=r.get("form"),
                mark_at=r.get("at"), pin=r.get("pin")))
    fact = {"ok": ok, "wrong": len(vios),
            "not_judged": sum(len(v) for v in unjudged.values()),
            "not_judged_refs": [{"why": w, "refs": refs}
                                for w, refs in unjudged.items()]}
    return vios, fact


# ------------------------------------------------------------ CLI

def run(argv=None):
    ap = argparse.ArgumentParser(
        description="Silkscreen over-pad / legibility check.")
    ap.add_argument("--pcb", required=True, help="path to .kicad_pcb")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)

    bg = geom.load_board(Path(args.pcb))
    root = sexpdata.loads(Path(args.pcb).read_text(encoding="utf-8"))
    silks = parse_silk(root)
    violations = run_checks(bg, silks)
    violations.extend(check_attribution(bg, root))
    violations.extend(check_pin_labels(bg, silks))
    violations.extend(check_label_nets(bg, silks))
    pol_vios, polarity = check_polarity_marks(Path(args.pcb))
    violations.extend(pol_vios)

    off_silk = refdes_off_silk(root)
    payload = checklib.report(
        SCRIPT, args.pcb, violations,
        checked=[{"silk_items": len(silks),
                  "texts": sum(1 for s in silks if s.kind == "text"),
                  "graphics": sum(1 for s in silks if s.kind != "text"),
                  "refdes_checked": len(refdes_texts(root)),
                  "refdes_off_silk": len(off_silk)}],
        refdes_off_silk=off_silk,
        polarity_marks=polarity,
        nothing_to_judge=None if violations or silks or refdes_texts(root)
        else "the board has no silk and no reference text")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
