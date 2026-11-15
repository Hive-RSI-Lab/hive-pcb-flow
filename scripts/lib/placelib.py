"""placelib - placement model + metrics shared by place_seed/place_metrics/place_edit (S9).

Parses .kicad_pcb footprint blocks into a lightweight MOVABLE model that geom.py
deliberately does not keep (geom stores absolute pad geometry only):

  Footprint: ref, fpid, pos, angle (file convention), side, attr flags, locked,
             pads with LOCAL offsets, courtyard polygon in the LOCAL frame.

Absolute geometry is derived as  abs = fp_pos + R(-fp_angle) . local  (the
S3-verified transform, LEARNINGS [geometry][kicad]); a flipped footprint's flip
is baked into the file (locals mirrored, layers renamed B.*) so the same
transform covers both sides and the parser must NOT mirror anything.

Board outline / rule areas / net-indexed copper come from geom.load_board -
placelib does not duplicate that parsing.

Placement targeting works on the courtyard CENTER, not the footprint origin:
for asymmetric parts the origin can sit far outside the body (prior-attempt
fact: a 1x20 header's origin is ~24 mm off center), so seat placements compute
the origin from the desired center at the chosen rotation.

Everything is mm / mm^2; angles are file-convention degrees.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

import geom
from geom import _head, _is_node, _kid, _kids, _nums, _pts, _rot, _strs, _tok
import checklib

SOURCE = "check.place"
EPS_AREA = 0.01  # mm^2 - below this an intersection is numerical noise
# the least courtyard excess beyond the pads KiCad's library draws (chips
# under 0603: 0.15 mm; 0603 and up 0.25 mm), and the slack for its rounding
KICAD_MIN_EXCESS = 0.15
EXCESS_TOL = 0.005


# ---------------------------------------------------------------- model

@dataclass
class FpPad:
    number: str
    net: str | None
    local: tuple[float, float]  # offset from fp anchor, pre-rotation frame
    size: tuple[float, float]
    through: bool
    rot: float = 0.0            # rotation RELATIVE to the footprint, degrees.
    # The file stores the pad angle CUMULATIVE with the footprint angle
    # (machine-verified: every rotated 2-pad part carries pad ROT == fp angle),
    # so the parser captures (file_rot - fp_angle); the relative value stays
    # valid when the model later rotates the footprint via place_center.


@dataclass
class Footprint:
    ref: str
    fpid: str
    pos: tuple[float, float]
    angle: float                # file-convention degrees
    side: str                   # "front" | "back"  (from (layer F.Cu/B.Cu))
    attrs: frozenset[str]       # tokens of (attr ...): smd/through_hole/board_only/...
    locked: bool
    pads: list[FpPad]
    courtyard_local: Polygon | None   # local frame, the part's own side
    courtyard_missing: bool = False   # True when extents fall back to pads
    # how far a pad field grows for placement legality: 0.25 for what hwde
    # places; constraints placement.pad_margin_mm lowers it for an imported
    # layout (hw-lab eda-crossover: SparkFun's 0402s 0.2 mm pad to pad)
    pad_margin: float = 0.25

    # -- derived, local frame -------------------------------------------
    def _pad_box_local(self) -> Polygon | None:
        """Pad-field bbox with PER-PAD ROTATION applied (T6, ladder rows
        69+110: a ROT-90 SOT-23 pad (size 0.7 1.25) must yield a 1.25 mm-wide
        box - the unrotated read under-reported 0.275 mm per side)."""
        if not self.pads:
            return None
        m = self.pad_margin
        x0 = y0 = math.inf
        x1 = y1 = -math.inf
        for p in self.pads:
            th = math.radians(p.rot)
            c, s = abs(math.cos(th)), abs(math.sin(th))
            hx = p.size[0] / 2 * c + p.size[1] / 2 * s
            hy = p.size[0] / 2 * s + p.size[1] / 2 * c
            x0 = min(x0, p.local[0] - hx)
            y0 = min(y0, p.local[1] - hy)
            x1 = max(x1, p.local[0] + hx)
            y1 = max(y1, p.local[1] + hy)
        return box(x0 - m, y0 - m, x1 + m, y1 + m)

    def pad_shape_local(self) -> Polygon | None:
        """PRECISE pad field: the UNION of per-pad boxes, each grown 0.25 mm
        (rotation-aware like _pad_box_local). Unlike the bbox, this leaves
        pad-FREE regions (an LQFP ring's empty corners/interior) out - the
        bbox flagged tight-but-legal decouplers (pad-edge gap >= the S14
        0.62 mm rule, KiCad DRC clean) as courtyard overlaps on real boards
        (U5: stm32-blinky C1-C3/U1, measured gaps 0.66-1.82 mm). A part ON
        the pin tips still overlaps the per-pad boxes, so the S14 shorting
        class stays caught."""
        if not self.pads:
            return None
        m = self.pad_margin
        boxes = []
        for p in self.pads:
            th = math.radians(p.rot)
            c, s = abs(math.cos(th)), abs(math.sin(th))
            hx = p.size[0] / 2 * c + p.size[1] / 2 * s
            hy = p.size[0] / 2 * s + p.size[1] / 2 * c
            boxes.append(box(p.local[0] - hx - m, p.local[1] - hy - m,
                             p.local[0] + hx + m, p.local[1] + hy + m))
        return unary_union(boxes)

    def pad_copper_local(self) -> Polygon | None:
        """The pads themselves, ungrown: the union of per-pad boxes,
        rotation-aware like pad_shape_local."""
        if not self.pads:
            return None
        boxes = []
        for p in self.pads:
            th = math.radians(p.rot)
            c, s = abs(math.cos(th)), abs(math.sin(th))
            hx = p.size[0] / 2 * c + p.size[1] / 2 * s
            hy = p.size[0] / 2 * s + p.size[1] / 2 * c
            boxes.append(box(p.local[0] - hx, p.local[1] - hy,
                             p.local[0] + hx, p.local[1] + hy))
        return unary_union(boxes)

    def courtyard_holds_pads(self, excess: float = KICAD_MIN_EXCESS) -> bool:
        """True when the drawn courtyard already holds the pad field grown
        `excess` mm with round corners: a proper courtyard, as KiCad's
        library draws them (0.15 mm beyond the pads for chips under 0603,
        0.25 mm above). An EasyEDA body-only courtyard (#438) is not."""
        cy, pads = self.courtyard_local, self.pad_copper_local()
        if cy is None or pads is None:
            return False
        return cy.buffer(1e-6).contains(pads.buffer(excess - EXCESS_TOL))

    def judge_extents_local(self, margin: float | None = None):
        """The shape the place gate's part-vs-part overlap test judges
        (#1062), local frame. A proper courtyard (courtyard_holds_pads) is
        judged as drawn, as KiCad DRC judges it: growing its pads a further
        0.25 mm false-flagged stock 0402s 0.3 mm apart whose courtyards are
        clear. Otherwise the courtyard united with the pads grown
        pad_margin with ROUND corners, so a diagonal pair is judged by its
        true distance. `margin` (placement.pad_margin_mm) is an explicit
        grow and applies to every part."""
        cy, pads = self.courtyard_local, self.pad_copper_local()
        if margin is None and self.courtyard_holds_pads():
            return cy
        m = self.pad_margin if margin is None else margin
        grown = pads.buffer(m) if pads is not None else None
        if cy is not None:
            return cy if grown is None else cy.union(grown)
        return grown if grown is not None else box(-0.5, -0.5, 0.5, 0.5)

    def judge_extents_abs(self, margin: float | None = None):
        p = affinity.rotate(self.judge_extents_local(margin), -self.angle,
                            origin=(0, 0))
        return affinity.translate(p, self.pos[0], self.pos[1])

    def pad_copper_abs(self):
        cu = self.pad_copper_local()
        if cu is None:
            return None
        p = affinity.rotate(cu, -self.angle, origin=(0, 0))
        return affinity.translate(p, self.pos[0], self.pos[1])

    def courtyard_abs(self):
        if self.courtyard_local is None:
            return None
        p = affinity.rotate(self.courtyard_local, -self.angle, origin=(0, 0))
        return affinity.translate(p, self.pos[0], self.pos[1])

    def precise_extents_abs(self):
        """Declared courtyard UNION precise pad shape, board frame - the
        pairwise OVERLAP-test shape (U5). extents_abs stays the conservative
        Polygon hull for containment/edges/keepouts/packing, where covering
        more is the safe direction; for part-vs-part overlap it is the
        false-positive direction. May return a MultiPolygon."""
        pads = self.pad_shape_local()
        cy = self.courtyard_local
        if cy is not None:
            shape = cy if pads is None else cy.union(pads)
        elif pads is not None:
            shape = pads
        else:
            shape = box(-0.5, -0.5, 0.5, 0.5)
        p = affinity.rotate(shape, -self.angle, origin=(0, 0))
        return affinity.translate(p, self.pos[0], self.pos[1])

    def extents_local(self) -> Polygon:
        """EFFECTIVE courtyard: the declared courtyard expanded to at least
        cover the pad field (+0.25 mm); pad-bbox fallback when absent.

        S14 finding: EasyEDA courtyards can be SMALLER than the pad field
        (LQFP48 body-only rect, LED0805) - courtyard-only legality passed a
        board with 9 SHORTING pad pairs. A proper courtyard (stock KiCad:
        pads + margin) contains its pad box, so the union is a no-op there.
        """
        pad_box = self._pad_box_local()
        if self.courtyard_local is not None:
            if pad_box is None:
                return self.courtyard_local
            if self.courtyard_local.contains(pad_box):
                return self.courtyard_local
            merged = self.courtyard_local.union(pad_box)
            return merged if merged.geom_type == "Polygon" \
                else merged.convex_hull
        if pad_box is None:
            return box(-0.5, -0.5, 0.5, 0.5)
        return pad_box

    def center_local(self) -> tuple[float, float]:
        c = self.extents_local().centroid
        return (c.x, c.y)

    # -- derived, board frame -------------------------------------------
    def to_abs(self, local_xy: tuple[float, float]) -> tuple[float, float]:
        dx, dy = _rot(local_xy[0], local_xy[1], -self.angle)
        return (self.pos[0] + dx, self.pos[1] + dy)

    def extents_abs(self) -> Polygon:
        p = affinity.rotate(self.extents_local(), -self.angle, origin=(0, 0))
        return affinity.translate(p, self.pos[0], self.pos[1])

    def center_abs(self) -> tuple[float, float]:
        return self.to_abs(self.center_local())

    def pad_centers_abs(self) -> list[tuple[str, str | None, float, float]]:
        out = []
        for p in self.pads:
            x, y = self.to_abs(p.local)
            out.append((p.number, p.net, x, y))
        return out

    def place_center(self, center: tuple[float, float], angle: float) -> None:
        """Set angle and move the ORIGIN so the courtyard center lands on center."""
        self.angle = angle
        cx, cy = self.center_local()
        dx, dy = _rot(cx, cy, -angle)
        self.pos = (center[0] - dx, center[1] - dy)

    def mirror(self) -> None:
        """Flip the part to the other side IN MEMORY: mirror the LOCAL frame
        in y, toggle `side`. Position is untouched; the ANGLE is the caller's.

        This is what `pcbnew.FOOTPRINT.Flip(GetPosition(), TOP_BOTTOM)` writes
        to the file - the direction lib/place_swig.py uses precisely because it
        is the angle-INDEPENDENT one (LEFT_RIGHT keeps the orientation and so
        mirrors the local frame about an angle-dependent axis; see the note
        there). Measured on KiCad 10.0.3, usbbuck4 J1 at -90 deg: pad locals
        come back with y negated and x intact, angle -90 -> 90. LEARNINGS
        2026-07-11 [geometry][kicad] recorded the shape of the rewrite and
        flagged it corpus-unvalidated; U19 validated it on every movable part
        of that board.

        Because the flip is baked into the LOCALS, the same
        abs = pos + R(-angle).local transform keeps covering both sides and no
        other model code needs a side case - which is why place_edit can
        reproduce a mirrored model state with a plain absolute `place` op
        carrying `side`.

        KiCad also negates the footprint angle on flip; callers that re-seat
        the part with place_center(center, angle) set the angle absolutely
        afterwards, so this method deliberately leaves it alone.

        Involutive: mirror() twice restores the input exactly.
        """
        self.side = "back" if self.side == "front" else "front"
        self.pads = [FpPad(p.number, p.net, (p.local[0], -p.local[1]),
                           p.size, p.through, rot=(-p.rot) % 360.0)
                     for p in self.pads]
        if self.courtyard_local is not None:
            self.courtyard_local = affinity.scale(
                self.courtyard_local, xfact=1.0, yfact=-1.0, origin=(0, 0))

    @property
    def is_movable(self) -> bool:
        return not (self.locked or "board_only" in self.attrs)

    @property
    def is_mechanical(self) -> bool:
        """Not assembled: excluded from both the BOM and the pos files (a
        mounting hole). It has no fanout to route, so the seed lets it sit
        as close to the edge as its copper allows (edge_keep), not
        edge_margin in."""
        return {"exclude_from_bom", "exclude_from_pos_files"} <= self.attrs

    def edge_keep(self, cu_edge: float) -> float:
        """How far (mm) the effective courtyard must stay inside the outline
        so every pad's copper clears `cu_edge`, the board's copper-to-edge
        rule. An M3 hole's 5.0 mm pad in a 5.5 mm courtyard needs 0.05 mm
        against a 0.3 mm rule; a courtyard kissing the edge would leave the
        pad 0.25 mm off it, a copper_edge_clearance DRC error."""
        pb = self._pad_box_local()
        if pb is None:
            return 0.0
        px0, py0, px1, py1 = pb.buffer(-0.25, join_style=2).bounds
        ex0, ey0, ex1, ey1 = self.extents_local().bounds
        gap = min(px0 - ex0, py0 - ey0, ex1 - px1, ey1 - py1)
        return max(0.0, cu_edge - gap)


class PlaceModel:
    """The movable-footprint view of a board + the geom view for everything else."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.bg = geom.load_board(self.path)
        self.outline: Polygon = self.bg.outline
        self.rule_areas = self.bg.rule_areas
        self.footprints: dict[str, Footprint] = {}
        self._parse(self.path)

    # ------------------------------------------------------------ parsing
    def _parse(self, path: Path) -> None:
        import sexpdata
        tree = sexpdata.loads(path.read_text(encoding="utf-8"))
        for fp in _kids(tree, "footprint"):
            f = self._parse_fp(fp)
            if f is not None:
                self.footprints[f.ref] = f

    def _parse_fp(self, fp) -> Footprint | None:
        strs = _strs(fp)
        fpid = strs[0] if strs else ""
        at = _kid(fp, "at")
        nums = _nums(at) if at is not None else [0.0, 0.0]
        pos = (nums[0], nums[1])
        angle = nums[2] if len(nums) > 2 else 0.0
        layer = _kid(fp, "layer")
        side = "back" if (layer is not None and _strs(layer)
                          and _strs(layer)[0].startswith("B.")) else "front"

        ref = None
        for prop in _kids(fp, "property"):
            s = _strs(prop)
            if len(s) >= 2 and s[0] == "Reference":
                ref = s[1]
                break
        if not ref:
            return None

        attrs = set()
        attr = _kid(fp, "attr")
        if attr is not None:
            attrs = {_tok(t) for t in attr[1:] if not _is_node(t)}
        locked = False
        lk = _kid(fp, "locked")
        if lk is not None and (len(lk) < 2 or _tok(lk[1]) != "no"):
            locked = True
        if any(not _is_node(t) and _tok(t) == "locked" for t in fp[1:]):
            locked = True

        pads = []
        for pad in _kids(fp, "pad"):
            ps = _strs(pad)
            number = ps[0] if ps else ""
            toks = [_tok(t) for t in pad[1:4] if not _is_node(t)]
            through = any(t in ("thru_hole", "np_thru_hole") for t in toks)
            pat = _kid(pad, "at")
            pn = _nums(pat) if pat is not None else [0.0, 0.0]
            psz = _kid(pad, "size")
            sz = _nums(psz) if psz is not None else [0.0, 0.0]
            net = None
            pnet = _kid(pad, "net")
            if pnet is not None:
                ns = _strs(pnet)
                net = ns[-1] if ns else None
            # pad (at x y ROT): ROT is cumulative with the footprint angle
            # (absent token = absolute 0); store the RELATIVE rotation
            pad_abs = pn[2] if len(pn) > 2 else 0.0
            pads.append(FpPad(number, net, (pn[0], pn[1]),
                              (sz[0] if sz else 0.0, sz[1] if len(sz) > 1 else 0.0),
                              through, rot=(pad_abs - angle) % 360.0))

        want = "B.CrtYd" if side == "back" else "F.CrtYd"
        courtyard = _courtyard_poly(fp, want)
        return Footprint(ref=ref, fpid=fpid, pos=pos, angle=angle, side=side,
                         attrs=frozenset(attrs), locked=locked, pads=pads,
                         courtyard_local=courtyard,
                         courtyard_missing=courtyard is None)

    # ------------------------------------------------------------ queries
    def movable(self) -> list[Footprint]:
        return [f for f in self.footprints.values() if f.is_movable]

    def nets_with_pads(self) -> dict[str, list[tuple[float, float]]]:
        """net -> absolute pad centers (2+ pads only), sorted deterministically."""
        acc: dict[str, list[tuple[float, float]]] = {}
        for ref in sorted(self.footprints):
            for _num, net, x, y in self.footprints[ref].pad_centers_abs():
                if net:
                    acc.setdefault(net, []).append((round(x, 4), round(y, 4)))
        return {n: sorted(pts) for n, pts in sorted(acc.items()) if len(pts) >= 2}


def _courtyard_poly(fp_node, layer_name: str) -> Polygon | None:
    """Union of courtyard graphics on layer_name, in the footprint-local frame."""
    areas, lines = [], []
    for head in ("fp_rect", "fp_line", "fp_circle", "fp_poly", "fp_arc"):
        for g in _kids(fp_node, head):
            lay = _kid(g, "layer")
            if lay is None or not _strs(lay) or _strs(lay)[0] != layer_name:
                continue
            if head == "fp_rect":
                s, e = _nums(_kid(g, "start")), _nums(_kid(g, "end"))
                areas.append(box(min(s[0], e[0]), min(s[1], e[1]),
                                 max(s[0], e[0]), max(s[1], e[1])))
            elif head == "fp_circle":
                c, e = _nums(_kid(g, "center")), _nums(_kid(g, "end"))
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                if r > 0:
                    areas.append(Point(c[0], c[1]).buffer(r, quad_segs=16))
            elif head == "fp_poly":
                pts = _pts(_kid(g, "pts"))
                if len(pts) >= 3:
                    areas.append(Polygon(pts))
            elif head == "fp_line":
                s, e = _nums(_kid(g, "start")), _nums(_kid(g, "end"))
                lines.append(LineString([(s[0], s[1]), (e[0], e[1])]))
            else:  # fp_arc
                s = _nums(_kid(g, "start"))
                m = _nums(_kid(g, "mid"))
                e = _nums(_kid(g, "end"))
                pts = geom._arc_points((s[0], s[1]), (m[0], m[1]), (e[0], e[1]))
                lines.append(LineString(pts))
    if lines:
        rings = list(polygonize(unary_union(lines)))
        if rings:
            areas.extend(rings)
        elif not areas:
            hull = unary_union(lines).convex_hull
            if hull.geom_type == "Polygon" and hull.area > EPS_AREA:
                areas.append(hull)
    if not areas:
        return None
    u = unary_union(areas)
    if u.geom_type == "MultiPolygon":  # disjoint courtyard pieces: take hull
        u = u.convex_hull
    return u if (u.geom_type == "Polygon" and u.area > EPS_AREA) else None


# ---------------------------------------------------------------- net classes

def class_sets(constraints: dict | None, decoupling: dict | None):
    """(gnd_nets, power_nets) from constraints.power + decoupling associations.

    Single source for the net-class partition (T6 P6A-1): the annealer's
    crossing term excludes gnd-class nets (a pour crosses anything), so every
    consumer that measures crossings "signal" must use the same partition.
    (Its congestion demand charges every non-plane net its track pitch.)
    """
    power = {p.get("net") for p in (constraints or {}).get("power", [])}
    power |= {a.get("rail") for a in (decoupling or {}).get("associations", [])}
    power.discard(None)
    gnd = {"GND"} | {a.get("gnd", "GND")
                     for a in (decoupling or {}).get("associations", [])}
    return gnd, power


# ---------------------------------------------------------------- clusters

@dataclass
class Satellite:
    ref: str
    target_pin: tuple[str, str] | None  # (anchor_ref, pad_number) or None
    # a NESTED satellite (#431): its own anchor (`parent`, also named in
    # target_pin) is itself a satellite of the cluster - a decoupler of an
    # IC that is a group member. It rides the cluster, slotted at its
    # parent's pin once the parent has a slot.
    parent: str | None = None


@dataclass
class Cluster:
    anchor: str
    satellites: list[Satellite] = field(default_factory=list)
    edge: dict | None = None    # placement.edges entry pinned to this cluster
    template: str | None = None  # placement.groups[].template ("crystal", ...)
    fixed: bool = False         # anchor never moves (locked / placement.fixed)

    @property
    def refs(self) -> list[str]:
        return [self.anchor] + [s.ref for s in self.satellites]


def build_clusters(model: PlaceModel, decoupling: dict | None,
                   placement: dict | None, *, with_fixed: bool = False
                   ) -> tuple[list[Cluster], list[str]]:
    """Satellite clusters from decoupling associations + explicit placement
    groups; every remaining movable footprint is a singleton. Returns
    (clusters sorted by anchor, warnings). Missing refs warn, never raise.

    A satellite of a satellite (an IC group member's decoupler) is folded
    into the top cluster as a NESTED satellite (Satellite.parent), in
    whichever order the associations arrive (#431). A part that never moves
    (locked, board_only, placement.fixed) is never claimed as a satellite.
    with_fixed: also return a Cluster(fixed=True) for every fixed anchor
    that has satellites, so the caller can place them round it (#431);
    without it those satellites are in no cluster."""
    warnings: list[str] = []
    fps = model.footprints
    fixed_extra = set((placement or {}).get("fixed", []))
    owner: dict[str, str] = {}          # satellite ref -> anchor ref
    sat_of: dict[str, list[Satellite]] = {}

    def claim(sat: str, anchor: str, pin: str | None, src: str) -> None:
        if sat not in fps or anchor not in fps:
            missing = sat if sat not in fps else anchor
            warnings.append(f"{src}: ref {missing} not on board - ignored")
            return
        if not fps[sat].is_movable or sat in fixed_extra:
            return
        if sat in owner:
            if owner[sat] != anchor:
                warnings.append(f"{src}: {sat} already satellite of {owner[sat]}"
                                f" - keeping first association")
            return
        up = anchor
        while up in owner:
            up = owner[up]
            if up == sat:
                warnings.append(f"{src}: {sat} -> {anchor} would make a "
                                f"satellite loop - {sat} ignored")
                return
        owner[sat] = anchor
        sat_of.setdefault(anchor, []).append(
            Satellite(sat, (anchor, pin) if pin else None))

    for a in (decoupling or {}).get("associations", []):
        cap, ic, pin = a.get("cap"), a.get("ic"), a.get("pin")
        if cap and ic:
            claim(cap, ic, str(pin) if pin is not None else None, "decoupling")
    tmpl_of: dict[str, str] = {}
    for g in (placement or {}).get("groups", []):
        anchor = g.get("anchor")
        if anchor and g.get("template"):
            tmpl_of[anchor] = g["template"]
        for m in g.get("members", []):
            claim(m, anchor, None, f"group {g.get('name', anchor)}")

    def top(ref: str) -> str:
        while ref in owner:
            ref = owner[ref]
        return ref

    def depth(ref: str) -> int:
        d = 0
        while ref in owner:
            ref, d = owner[ref], d + 1
        return d

    # fold nested satellites into their top cluster, parents first
    members: dict[str, list[Satellite]] = {}
    for anchor in sorted(sat_of):
        t = top(anchor)
        for s in sat_of[anchor]:
            if anchor != t:
                s.parent = anchor
            members.setdefault(t, []).append(s)
    for t in members:
        members[t].sort(key=lambda s: (depth(s.ref), s.ref))

    edges = {e["ref"]: e for e in (placement or {}).get("edges", [])
             if e.get("ref")}

    clusters, fixed = [], []
    for ref in sorted(fps):
        f = fps[ref]
        if not f.is_movable or ref in fixed_extra:
            fixed.append(ref)
            if with_fixed and members.get(ref):
                clusters.append(Cluster(anchor=ref, satellites=members[ref],
                                        template=tmpl_of.get(ref),
                                        fixed=True))
            continue
        if ref in owner:
            continue
        c = Cluster(anchor=ref, satellites=members.get(ref, []),
                    template=tmpl_of.get(ref))
        anchor_edge = edges.get(ref)
        for s in c.satellites:
            if s.ref in edges and anchor_edge is None:
                anchor_edge = edges[s.ref]
                warnings.append(f"edge for satellite {s.ref} pins its cluster "
                                f"anchor {ref}")
        c.edge = anchor_edge
        clusters.append(c)
    for ref in sorted(edges):
        if ref not in fps:
            warnings.append(f"placement.edges: ref {ref} not on board - ignored")
    return clusters, warnings


# ---------------------------------------------------------------- legality

def _forbidden(model: PlaceModel, placement: dict | None, side: str):
    """Keepout polygons for footprints on side: constraints keepouts + board
    rule areas that forbid footprints and whose layers include that side's
    copper (conservative). A rule area whose footprints flag is "allowed"
    only scopes a rule (e.g. a track width) and is not a placement keepout."""
    polys = []
    for k in (placement or {}).get("keepouts", []):
        ks = k.get("side", "both")
        if ks not in ("both", side):
            continue
        if "rect" in k:
            x1, y1, x2, y2 = k["rect"]
            polys.append((box(min(x1, x2), min(y1, y2), max(x1, x2),
                              max(y1, y2)), k.get("reason", "keepout")))
        elif "poly" in k:
            polys.append((Polygon([tuple(p) for p in k["poly"]]),
                          k.get("reason", "keepout")))
    want = "F.Cu" if side == "front" else "B.Cu"
    for ra in model.rule_areas:
        if (ra.get("flags") or {}).get("footprints") != "not_allowed":
            continue
        lays = ra.get("layers") or []
        if want in lays or "*.Cu" in lays or not lays:
            polys.append((ra["outline"], f"rule area '{ra.get('name') or '?'}'"))
    return polys


# A declared-edge part's courtyard must come within this of its edge. 2.5 mm
# accepts a sensibly-inboard THT header (the usbbuck4 golden sits ~2 mm in)
# while still failing a connector stranded mid-board.
EDGE_TOL = 2.5  # mm

# Minimum courtyard fraction that must stay on the board for a declared-edge
# part. Calibrated on the rf4 golden: its edge-mount SMA (which CLAMPS the
# board edge) keeps only 35% of its courtyard on-board by design.
ON_BOARD_MIN = 0.25


# KiCad's own default for min_copper_edge_clearance, used when the board's
# .kicad_pro does not set one.
COPPER_EDGE_DEFAULT = 0.5  # mm


def copper_edge_mm(model: PlaceModel) -> float:
    """The board's copper-to-edge rule: .kicad_pro
    board.design_settings.rules.min_copper_edge_clearance beside the pcb."""
    try:
        pro = json.loads(model.path.with_suffix(".kicad_pro").read_text(
            encoding="utf-8"))
        v = pro["board"]["design_settings"]["rules"][
            "min_copper_edge_clearance"]
        return float(v)
    except (OSError, ValueError, KeyError, TypeError):
        return COPPER_EDGE_DEFAULT

def edge_line(outline: Polygon, edge: str) -> LineString:
    minx, miny, maxx, maxy = outline.bounds
    return {
        "left": LineString([(minx, miny), (minx, maxy)]),
        "right": LineString([(maxx, miny), (maxx, maxy)]),
        "top": LineString([(minx, miny), (maxx, miny)]),
        "bottom": LineString([(minx, maxy), (maxx, maxy)]),
    }[edge]


def legality_violations(model: PlaceModel, placement: dict | None) -> list[dict]:
    """Normalized violations (S2 schema): courtyard overlaps, outline
    containment, keepouts, declared-edge compliance, missing courtyards."""
    v: list[dict] = []
    placement = placement or {}
    edges = {e["ref"]: e for e in placement.get("edges", []) if e.get("ref")}
    fps = [model.footprints[r] for r in sorted(model.footprints)]
    margin = None
    if placement.get("pad_margin_mm") is not None:
        margin = float(placement["pad_margin_mm"])
        for f in fps:
            f.pad_margin = margin
    ext = {f.ref: f.extents_abs() for f in fps}

    def collides(a: Footprint, b: Footprint) -> bool:
        # not assembled and nothing through the board (a logo, a silk label, a
        # fiducial, wire pads): nothing to place, so nothing to collide with.
        # DRC holds their copper. A mounting hole is through: it still counts.
        for f in (a, b):
            if f.is_mechanical and not any(p.through for p in f.pads):
                return False
        if a.side == b.side:
            return True
        thru = ("through_hole" in a.attrs or any(p.through for p in a.pads)
                or "through_hole" in b.attrs or any(p.through for p in b.pads))
        return thru

    # Overlap uses the JUDGE shape (#1062): a proper drawn courtyard as
    # drawn, else courtyard + pads grown round (U5: the bbox hull's pad-free
    # corners false-flagged tight-but-legal decouplers). The hull contains
    # it, so hull-disjoint pairs skip the precise intersection outright.
    # The placer still packs by precise_extents_abs, the stricter shape.
    precise: dict = {}

    def prec(f: Footprint):
        if f.ref not in precise:
            precise[f.ref] = f.judge_extents_abs(margin)
        return precise[f.ref]

    def overlap_msg(a: Footprint, b: Footprint, area: float):
        ca, cb = a.courtyard_abs(), b.courtyard_abs()
        if ca is not None and cb is not None \
                and ca.intersection(cb).area <= EPS_AREA:
            pa, pb = a.pad_copper_abs(), b.pad_copper_abs()
            gap = pa.distance(pb) if pa is not None and pb is not None \
                else None
            ma = a.pad_margin if margin is not None \
                or not a.courtyard_holds_pads() else 0.0
            mb = b.pad_margin if margin is not None \
                or not b.courtyard_holds_pads() else 0.0
            gs = f"{gap:.2f} mm" if gap is not None else "?"
            return (f"pad fields too close {a.ref}/{b.ref}: drawn courtyards "
                    f"clear, pads {gs} apart, pads grown {ma:g}/{mb:g} mm "
                    f"overlap ({area:.2f} mm2)"), {
                        "drawn_courtyards_clear": True,
                        "pad_gap_mm": checklib.rnd(gap) if gap is not None
                        else None,
                        "pad_margin_mm": [ma, mb]}
        return f"courtyard overlap {a.ref}/{b.ref} ({area:.2f} mm2)", {}

    for i, a in enumerate(fps):
        for b in fps[i + 1:]:
            if not collides(a, b):
                continue
            if not ext[a.ref].intersects(ext[b.ref]):
                continue
            inter = prec(a).intersection(prec(b))
            if inter.area > EPS_AREA:
                c = inter.centroid
                msg, extra = overlap_msg(a, b, inter.area)
                v.append(checklib.violation(
                    "place", "error", (c.x, c.y), None, None, [a.ref, b.ref],
                    msg, SOURCE, kind="courtyard_overlap",
                    overlap_mm2=checklib.rnd(inter.area), **extra))

    for f in fps:
        e = ext[f.ref]
        outside = e.difference(model.outline)
        decl = edges.get(f.ref)
        if decl:
            line = edge_line(model.outline, decl["edge"])
            if e.distance(line) > EDGE_TOL:
                c = e.centroid
                v.append(checklib.violation(
                    "place", "error", (c.x, c.y), None, None, [f.ref],
                    f"{f.ref} declared on edge '{decl['edge']}' but is "
                    f"{e.distance(line):.1f} mm away", SOURCE,
                    kind="edge_violation", edge=decl["edge"],
                    dist_mm=checklib.rnd(e.distance(line))))
            if outside.area > EPS_AREA:
                # overhang across the declared edge is allowed (connector
                # bodies legitimately hang off), but the part must still
                # meaningfully sit on the board
                if e.intersection(model.outline).area < ON_BOARD_MIN * e.area:
                    c = e.centroid
                    v.append(checklib.violation(
                        "place", "error", (c.x, c.y), None, None, [f.ref],
                        f"{f.ref} mostly outside the board outline", SOURCE,
                        kind="outside_outline",
                        outside_mm2=checklib.rnd(outside.area)))
        elif outside.area > EPS_AREA and f.is_movable and not f.is_mechanical:
            # board_only/locked infrastructure (mounting holes), artwork and
            # what isn't assembled (castellations, wire pads) may kiss the
            # outline by design - obstacles, not placement subjects
            c = e.centroid
            v.append(checklib.violation(
                "place", "error", (c.x, c.y), None, None, [f.ref],
                f"{f.ref} extends {outside.area:.2f} mm2 outside the board "
                f"outline", SOURCE, kind="outside_outline",
                outside_mm2=checklib.rnd(outside.area)))

        if f.is_movable:
            for poly, why in _forbidden(model, placement, f.side):
                inter = e.intersection(poly)
                if inter.area > EPS_AREA:
                    c = inter.centroid
                    v.append(checklib.violation(
                        "place", "error", (c.x, c.y), None, None, [f.ref],
                        f"{f.ref} intersects {why} ({inter.area:.2f} mm2)",
                        SOURCE, kind="keepout_violation",
                        overlap_mm2=checklib.rnd(inter.area)))
        if f.courtyard_missing and f.is_movable:
            v.append(checklib.violation(
                "place", "warning", f.center_abs(), None, None, [f.ref],
                f"{f.ref} has no courtyard - pad bbox +0.25 mm used", SOURCE,
                kind="courtyard_missing"))
    return v


def declared_assoc_dist(model: PlaceModel, assoc: dict):
    """(manhattan_mm, limit_mm) for one decoupling association carrying a
    DECLARED max_dist_mm, measured on the in-memory model (same pad-matching
    semantics as check_decoupling: cap rail pads x IC pin pads on the rail,
    nearest pair). None when the association declares no limit or no longer
    matches the model - stale metadata stays check_decoupling's report."""
    limit = assoc.get("max_dist_mm")
    if limit is None:
        return None
    cap = model.footprints.get(assoc.get("cap"))
    ic = model.footprints.get(assoc.get("ic"))
    if cap is None or ic is None:
        return None
    pin, rail = str(assoc.get("pin", "")), assoc.get("rail")
    cap_pads = [(x, y) for _n, net, x, y in cap.pad_centers_abs()
                if net == rail]
    pin_pads = [(x, y) for n, net, x, y in ic.pad_centers_abs()
                if n == pin and net == rail]
    if not cap_pads or not pin_pads:
        return None
    dist = min(abs(cx - px) + abs(cy - py)
               for cx, cy in cap_pads for px, py in pin_pads)
    return dist, float(limit)


def declared_decap_violations(model: PlaceModel, decoupling: dict | None
                              ) -> list[dict]:
    """ERROR violations for decoupling associations whose DECLARED
    max_dist_mm the model's placement exceeds (U20: the annealer discards any
    candidate carrying one - a declared limit is a hard contract, never a
    score)."""
    out: list[dict] = []
    for a in (decoupling or {}).get("associations", []):
        got = declared_assoc_dist(model, a)
        if got is None:
            continue
        dist, limit = got
        if dist <= limit + 1e-6:
            continue
        cap, ic = a["cap"], a["ic"]
        pos = model.footprints[cap].center_abs()
        out.append(checklib.violation(
            "place", "error", pos, None, a.get("rail"), [cap, ic],
            f"{cap} rail pad is {dist:.2f} mm Manhattan from {ic} pin "
            f"{a.get('pin')} against a declared {limit:g} mm limit", SOURCE,
            kind="decoupler_distance", limit_mm=limit,
            manhattan_mm=checklib.rnd(dist), declared=True))
    return out


# ---------------------------------------------------------------- metrics

def hpwl(model: PlaceModel) -> dict:
    per = {}
    for net, pts in model.nets_with_pads().items():
        xs, ys = zip(*pts)
        per[net] = checklib.rnd((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return {"total_mm": checklib.rnd(sum(per.values())), "by_net": per}


def _mst_edges(pts: list[tuple[float, float]]):
    """Prim MST over points; deterministic for a sorted input list."""
    n = len(pts)
    if n < 2:
        return []
    in_tree = [False] * n
    best = [(math.inf, -1)] * n
    in_tree[0] = True
    for j in range(1, n):
        best[j] = (math.dist(pts[0], pts[j]), 0)
    edges = []
    for _ in range(n - 1):
        j = min((j for j in range(n) if not in_tree[j]),
                key=lambda j: (best[j][0], j))
        edges.append((pts[best[j][1]], pts[j]))
        in_tree[j] = True
        for k in range(n):
            if not in_tree[k]:
                d = math.dist(pts[j], pts[k])
                if d < best[k][0]:
                    best[k] = (d, j)
    return edges


def flight_lines(model: PlaceModel) -> dict[str, list]:
    return {net: _mst_edges(pts)
            for net, pts in model.nets_with_pads().items()}


def crossings(model: PlaceModel, exclude: frozenset = frozenset()) -> dict:
    """Pairs of MST flight-line segments of DIFFERENT nets that cross.

    `exclude` skips nets entirely (T6 P6A-1: pass the gnd-class set for the
    "signal" variant - gnd flight lines ride planes and are never
    point-to-point routed, so counting them points the gradient at
    quantities no placement change should chase)."""
    segs = [(net, LineString(e)) for net, edges in flight_lines(model).items()
            if net not in exclude for e in edges if LineString(e).length > 1e-6]
    count, worst = 0, {}
    for i, (na, sa) in enumerate(segs):
        for nb, sb in segs[i + 1:]:
            if na != nb and sa.crosses(sb):
                count += 1
                key = tuple(sorted((na, nb)))
                worst[key] = worst.get(key, 0) + 1
    pairs = [{"nets": list(k), "crossings": c}
             for k, c in sorted(worst.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"count": count, "pairs": pairs[:20]}


def congestion(model: PlaceModel, cell_mm: float = 2.0,
               exclude: frozenset = frozenset()) -> dict:
    minx, miny, maxx, maxy = model.outline.bounds
    cols = max(1, math.ceil((maxx - minx) / cell_mm))
    rows = max(1, math.ceil((maxy - miny) / cell_mm))
    demand: dict[tuple[int, int], int] = {}
    for _net, edges in flight_lines(model).items():
        if _net in exclude:
            continue
        for (ax, ay), (bx, by) in edges:
            length = math.dist((ax, ay), (bx, by))
            steps = max(1, math.ceil(length / (cell_mm / 2)))
            cells = set()
            for s in range(steps + 1):
                t = s / steps
                x, y = ax + (bx - ax) * t, ay + (by - ay) * t
                i = min(cols - 1, max(0, int((x - minx) / cell_mm)))
                j = min(rows - 1, max(0, int((y - miny) / cell_mm)))
                cells.add((i, j))
            for c in cells:
                demand[c] = demand.get(c, 0) + 1
    hot = sorted(demand.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {"cell_mm": cell_mm, "cols": cols, "rows": rows,
            "max": max(demand.values()) if demand else 0,
            "mean_nonzero": checklib.rnd(sum(demand.values()) / len(demand))
            if demand else 0.0,
            "hotspots": [{"cell": list(c),
                          "center": [checklib.rnd(minx + (c[0] + .5) * cell_mm),
                                     checklib.rnd(miny + (c[1] + .5) * cell_mm)],
                          "demand": d} for c, d in hot]}


# ---------------------------------------------------------------- silk text

# Measured KiCad stroke-font metrics (LEARNINGS 2026-07-30 [place_edit]
# [placement][silk]: a 3-char refdes at size 1.0 / thickness 0.15 inks to
# ~2.64-2.69 x 1.16 mm, i.e. per-char advance ~0.845 * size and height
# size + thickness). Guessing 0.75 or 1.0 per char both give wrong answers.
TEXT_ADVANCE = 0.845


def text_box(text: str, size: float, thickness: float,
             x: float, y: float, deg: float = 0.0) -> Polygon:
    """Absolute-frame bounding box of a silk text.

    CRUCIAL (ladder row 145): a footprint text field's stored angle is
    ABSOLUTE board-frame - callers must pass it verbatim, never add the
    footprint rotation (adding both mis-rotates every rotated part's label
    obstacle)."""
    w = max(1, len(text)) * TEXT_ADVANCE * size + thickness
    h = size + thickness
    b = box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    return affinity.rotate(b, -deg, origin=(x, y)) if deg % 180.0 else b
