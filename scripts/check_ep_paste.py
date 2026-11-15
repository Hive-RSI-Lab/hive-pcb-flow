"""check_ep_paste.py - solder paste on exposed pads, and paste over holes
(P8 verify suite).

The hit (hifi-receiver-2 first-spin review, 2026-10-29, DFM-9): Lite's U8
had paste windows printed over the thermal vias in its exposed pad, so the
solder runs down the barrels, the pad voids and the part floats or starves
its ground joint. No gate looked: DRC has no rule for paste, and dfm's paste
leg only asks that every aperture has a mask opening. A reviewer measured it
by hand; this check measures it on every board.

Paste apertures are read from the board as JLC's stencil gets them: each
SMD pad on F.Paste/B.Paste grown by its paste margin (the pad's own, else
its footprint's, else the board's pad_to_paste_clearance, each as a margin
plus a ratio of the pad's size, as KiCad does), plus the paste-only pads a
footprint carries (a QFN's window-pane apertures), plus every graphic drawn
on F.Paste/B.Paste (a footprint's fp_poly/fp_rect/fp_circle/fp_line, or a
board gr_* shape): Eagle draws an exposed pad's windows on tCream with the
pad's own cream off, and KiCad's importer brings them in as F.Paste
graphics, which the stencil gerber carries (hive-hwde #544). A footprint's
graphic counts for its own exposed pad; a board graphic for any it covers.

  ep_paste_missing   error: an exposed pad with no paste at all - the
                     ground/thermal joint is never made
  ep_paste_low       warning: paste covers less than COVER_MIN of the pad
  ep_paste_high      warning: paste covers more than COVER_MAX of the pad -
                     the part floats on the pad, its leads open or bridge
                     (IPC-7093 and the vendors' notes: 50-80 %)
  paste_on_via       error: a via's hole lies under an exposed pad's paste
                     (its own aperture or a window over it), unless the via
                     is filled or capped (KiCad's via `filling`/`capping`).
                     A paste-only window has no number of its own, so it
                     is named by the exposed pad it lies on ("U1 pad 9's
                     paste window", pads "U1:9"); one over no exposed pad
                     is "U1 paste window", pads "U1:".
                     A via under any other pad's paste (via-in-pad on a
                     decoupler or an RF ground, which the golden boards
                     carry on purpose) is listed in the facts
                     (`vias_in_paste`), not failed: it wicks too, and can
                     tombstone a chip part, so a review weighs it

An exposed pad is a footprint's SMD copper pad numbered "EP", or one at
least EP_AREA_RATIO times the median of the footprint's other pads (four or
more of them) whose centre lies inside their hull: a QFN/DFN/SOIC-EP's
belly, never a SOT-223 tab at the edge of the pads. Only pads with copper
on the footprint's own side (its `(layer "F.Cu")` or `"B.Cu"`) count, as
exposed pads and as the other pads: a pad on the far side is never under
the part. KiCad's stock *_ThermalVias footprints carry such a bottom copy
of the belly for their vias to land on (hive-pcb-flow #1174). A footprint
with no layer keeps both sides. Each one's coverage goes into the facts
(`exposed_pads`) whether it passes or not.

CLI: --pcb board.kicad_pcb [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import sexpdata
from shapely import affinity
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import fpfix  # noqa: E402
from geom import (_kid, _kids, _nums, _pad_polygon, _pts, _rot, _strs,  # noqa: E402
                  _tok)

SCRIPT = "check_ep_paste"
SOURCE = SCRIPT  # verify_all, gate and the scorecard match a finding to its check by source
COVER_MIN = 0.35
COVER_MAX = 0.90
EP_AREA_RATIO = 4.0
# a hole reaching less than this (mm2) into an aperture is the arc sampling
HIT_MM2 = 1e-3


def _num(node, name: str) -> float | None:
    k = _kid(node, name)
    v = _nums(k) if k is not None else []
    return v[0] if v else None


def _yes(node, name: str) -> bool:
    k = _kid(node, name)
    return k is not None and any(_tok(t) == "yes" for t in k[1:])


class Ap:
    """A pad as the paste check sees it, in the board frame."""

    def __init__(self, ref, number, kind, shape, size, center, angle,
                 rratio, copper, paste, margin, prims, side=None):
        self.ref, self.number, self.kind = ref, number, kind
        self.shape, self.size, self.center = shape, size, center
        self.angle, self.rratio = angle, rratio
        self.copper, self.paste = copper, paste    # sides: {"F", "B"}
        self.margin, self.prims = margin, prims
        self.side = side    # the footprint's side, "F"/"B", None unknown

    def poly(self, grow=(0.0, 0.0)) -> Polygon:
        w, h = self.size[0] + 2 * grow[0], self.size[1] + 2 * grow[1]
        if w <= 0 or h <= 0:
            return Polygon()
        base = _pad_polygon(self.shape if self.shape != "custom" else "rect",
                            w, h, self.rratio, self.center, self.angle)
        if self.shape != "custom" or not self.prims:
            return base
        g = max(grow)
        parts = [base]
        for pts in self.prims:
            p = Polygon(pts)
            if g:
                p = p.buffer(g, join_style="mitre")
            p = affinity.rotate(p, -self.angle, origin=(0, 0))
            parts.append(affinity.translate(p, *self.center))
        return unary_union(parts)

    def copper_poly(self) -> Polygon:
        return self.poly()

    def paste_poly(self) -> Polygon:
        mx = self.margin[0] + self.size[0] * self.margin[1]
        my = self.margin[0] + self.size[1] * self.margin[1]
        return self.poly((mx, my))


class Gfx:
    """A paste-layer graphic as an aperture: no copper, no pad number."""
    kind, copper, number = "graphic", frozenset(), "graphic"

    def __init__(self, ref, sides, poly):
        self.ref, self.paste, self.poly = ref, sides, poly


_GFX = ("poly", "rect", "circle", "line", "arc")


def _gfx_poly(node, origin=(0.0, 0.0), angle=0.0):
    """The printed area of a graphic node (fill plus stroke), board frame."""
    head = str(_tok(node[0]))
    sh = fpfix.graphic_shape(["fp_" + head.split("_", 1)[1]] + list(node[1:]))
    if sh is None:
        return None
    w = fpfix._stroke_width(node) or 0.0
    area = sh if sh.geom_type == "Polygon" else Polygon()
    if w > 0:
        area = unary_union([area, sh.buffer(w / 2.0, quad_segs=8)])
    if area.is_empty or area.area <= 0:
        return None
    if angle:
        area = affinity.rotate(area, -angle, origin=(0, 0))
    return affinity.translate(area, *origin)


def _gfx_sides(node) -> set:
    lay = _strs(_kid(node, "layer")) if _kid(node, "layer") is not None else []
    return {s for s in ("F", "B") if f"{s}.Paste" in lay}


def _margin(node, keys) -> tuple[float | None, float | None]:
    return _num(node, keys[0]), next(
        (v for k in keys[1:] if (v := _num(node, k)) is not None), None)


def load(pcb: Path) -> tuple[list, list[dict]]:
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise checklib.CheckError(f"cannot parse {pcb}: {exc}") from exc
    setup = _kid(root, "setup")
    board = _margin(setup, ("pad_to_paste_clearance",
                            "pad_to_paste_clearance_ratio")) \
        if setup is not None else (None, None)
    pads: list[Ap] = []
    for fp in _kids(root, "footprint"):
        ref = "?"
        for prop in _kids(fp, "property"):
            s = _strs(prop)
            if len(s) >= 2 and s[0] == "Reference":
                ref = s[1]
        at = _nums(_kid(fp, "at"))
        fx, fy = at[0], at[1]
        fa = at[2] if len(at) > 2 else 0.0
        fpm = _margin(fp, ("solder_paste_margin", "solder_paste_ratio",
                           "solder_paste_margin_ratio"))
        fl = _kid(fp, "layer")
        fside = {"F.Cu": "F", "B.Cu": "B"}.get(
            str(_tok(fl[1])) if fl is not None and len(fl) > 1 else "")
        for pad in _kids(fp, "pad"):
            if len(pad) < 4:
                continue
            pat, psz = _kid(pad, "at"), _kid(pad, "size")
            if pat is None or psz is None:
                continue
            lx, ly, *rest = _nums(pat)
            dx, dy = _rot(lx, ly, -fa)
            sz = _nums(psz)
            lay = _strs(_kid(pad, "layers")) if _kid(pad, "layers") else []
            sides = lambda suffix: {s[0] for s in ("F", "B")  # noqa: E731
                                    if f"{s}.{suffix}" in lay
                                    or f"*.{suffix}" in lay}
            pm = _margin(pad, ("solder_paste_margin",
                               "solder_paste_margin_ratio"))
            # KiCad: the pad's own value unless 0/absent, then the
            # footprint's, then the board's - margin and ratio apart
            m = next((v for v in (pm[0], fpm[0], board[0]) if v), 0.0)
            r = next((v for v in (pm[1], fpm[1], board[1]) if v), 0.0)
            prims = []
            pr = _kid(pad, "primitives")
            if pr is not None:
                prims = [pts for g in _kids(pr, "gr_poly")
                         if len(pts := _pts(_kid(g, "pts"))) >= 3]
            rr = _kid(pad, "roundrect_rratio")
            pads.append(Ap(ref, str(pad[1]), str(_tok(pad[2])),
                           str(_tok(pad[3])), (sz[0], sz[-1]),
                           (fx + dx, fy + dy), rest[0] if rest else 0.0,
                           _nums(rr)[0] if rr is not None else 0.0,
                           sides("Cu"), sides("Paste"), (m, r), prims,
                           fside))
        for g in fp[1:]:
            if not (isinstance(g, list) and g) or \
                    str(_tok(g[0])) not in tuple("fp_" + k for k in _GFX):
                continue
            if (sd := _gfx_sides(g)) and \
                    (poly := _gfx_poly(g, (fx, fy), fa)) is not None:
                pads.append(Gfx(ref, sd, poly))
    for g in root[1:]:
        if isinstance(g, list) and g and \
                str(_tok(g[0])) in tuple("gr_" + k for k in _GFX) and \
                (sd := _gfx_sides(g)) and (poly := _gfx_poly(g)) is not None:
            pads.append(Gfx(None, sd, poly))
    vias = []
    for v in _kids(root, "via"):
        at, dr = _kid(v, "at"), _kid(v, "drill")
        if at is None or dr is None or not _nums(dr):
            continue
        vias.append({"at": tuple(_nums(at)[:2]), "drill": _nums(dr)[0],
                     "net": (_strs(_kid(v, "net")) or [None])[-1]
                     if _kid(v, "net") is not None else None,
                     "plugged": _yes(v, "filling") or _yes(v, "capping")})
    return pads, vias


def exposed_pads(pads: list[Ap]) -> list[Ap]:
    by_ref: dict[str, list[Ap]] = {}
    for p in pads:
        if p.kind == "smd" and p.copper and \
                (p.side is None or p.side in p.copper):
            by_ref.setdefault(p.ref, []).append(p)
    out = []
    for ref, ps in by_ref.items():
        for p in ps:
            if p.number.upper() == "EP":
                out.append(p)
                continue
            rest = [q for q in ps if q is not p]
            if len(rest) < 4:
                continue
            med = statistics.median(q.copper_poly().area for q in rest)
            if med <= 0 or p.copper_poly().area < EP_AREA_RATIO * med:
                continue
            hull = MultiPoint([q.center for q in rest]).convex_hull
            if hull.geom_type == "Polygon" and hull.contains(Point(p.center)):
                out.append(p)
    return out


def _aperture_name(p, poly, ep_cu: dict, at) -> tuple[str, str, str]:
    """(ref, pad, label) for an aperture over the via at `at`. A paste-only
    pad (a QFN's window-pane aperture, `(pad "" smd ...)` on F.Paste alone)
    has no number, so it takes the number of the exposed pad it lies on:
    the one that holds the via, else the one it overlaps most."""
    ref = p.ref or "board"
    if p.number == "graphic":
        return ref, "graphic", f"{ref} paste graphic"
    if p.copper or p.number:
        return ref, p.number, f"{ref} pad {p.number}"
    cands = [(cu.contains(Point(at)), cu.intersection(poly).area, n)
             for (r, n), cu in ep_cu.items()
             if r == p.ref and cu.intersects(poly)]
    if not cands:
        return ref, "", f"{ref} paste window"
    n = max(cands)[2]
    if not n:
        return ref, n, f"{ref} exposed pad's paste window"
    return ref, n, f"{ref} pad {n}'s paste window"


def check(pcb: Path) -> tuple[list[dict], dict]:
    pads, vias = load(pcb)
    viols: list[dict] = []
    # the stencil: every aperture per side, with the pad it came from
    apertures: dict[str, list[tuple[Ap, Polygon]]] = {"F": [], "B": []}
    for p in pads:
        if p.kind == "graphic":
            for side in p.paste:
                apertures[side].append((p, p.poly))
            continue
        if p.kind not in ("smd", "connect") or not p.paste:
            continue
        poly = p.paste_poly() if p.copper else p.copper_poly()
        if poly.is_empty or poly.area <= 0:
            continue
        for side in p.paste:
            apertures[side].append((p, poly))
    eps = []
    ep_list = exposed_pads(pads)
    ep_cu = {(ep.ref, ep.number): ep.copper_poly() for ep in ep_list}
    for ep in ep_list:
        side = ep.side or next(iter(sorted(ep.copper)))
        cu = ep.copper_poly()
        mine = [poly for p, poly in apertures[side]
                if (p.ref == ep.ref or p.ref is None)
                and (p is ep or not p.copper) and poly.intersects(cu)]
        paste = unary_union(mine).intersection(cu) if mine else Polygon()
        cover = paste.area / cu.area if cu.area else 0.0
        under = [v for v in vias if cu.contains(Point(v["at"]))]
        row = {"ref": ep.ref, "pad": ep.number, "side": side,
               "pad_mm2": checklib.rnd(cu.area, 3),
               "paste_mm2": checklib.rnd(paste.area, 3),
               "coverage": checklib.rnd(cover, 3), "apertures": len(mine),
               "graphic_apertures": sum(
                   1 for p, poly in apertures[side] if p.kind == "graphic"
                   and p.ref in (ep.ref, None) and poly.intersects(cu)),
               "vias_in_pad": len(under)}
        eps.append(row)
        layer = f"{side}.Paste"
        where = f"{ep.ref} pad {ep.number} (exposed, {cu.area:.2f} mm2)"
        if not mine:
            viols.append(checklib.violation(
                SCRIPT, "error", ep.center, layer, None, [ep.ref],
                f"{where} has no solder paste: its ground/thermal joint is "
                "never made. Give it paste windows (50-80 % of the pad, "
                "clear of its vias)", SOURCE, kind="ep_paste_missing",
                pad=ep.number, coverage=0.0))
        elif cover < COVER_MIN:
            viols.append(checklib.violation(
                SCRIPT, "warning", ep.center, layer, None, [ep.ref],
                f"{where}: paste covers {cover:.0%} of it (under "
                f"{COVER_MIN:.0%}): a starved thermal/ground joint",
                SOURCE, kind="ep_paste_low", pad=ep.number,
                coverage=checklib.rnd(cover, 3)))
        elif cover > COVER_MAX:
            viols.append(checklib.violation(
                SCRIPT, "warning", ep.center, layer, None, [ep.ref],
                f"{where}: paste covers {cover:.0%} of it (over "
                f"{COVER_MAX:.0%}): the part floats on the pad and its "
                "leads may open or bridge - split it into windows of "
                "50-80 %", SOURCE, kind="ep_paste_high", pad=ep.number,
                coverage=checklib.rnd(cover, 3)))
    hits, small = 0, []
    for v in vias:
        hole = Point(v["at"]).buffer(v["drill"] / 2.0, quad_segs=16)
        for side in ("F", "B"):
            under = [(p, poly) for p, poly in apertures[side]
                     if poly.intersection(hole).area > HIT_MM2]
            if not under:
                continue
            hits += 1
            if v["plugged"]:
                continue
            on = sorted({_aperture_name(p, poly, ep_cu, v["at"])
                         for p, poly in under})
            pad_ids = list(dict.fromkeys(f"{r}:{n}" for r, n, _ in on))
            in_ep = any((p.ref, p.number) in ep_cu or (
                not p.copper and any(p.ref in (r, None)
                                     and cu.intersects(poly)
                                     for (r, _), cu in ep_cu.items()))
                for p, poly in under)
            refs = sorted({r for r, _, _ in on})
            if not in_ep:
                small.append({"at": [checklib.rnd(c, 3) for c in v["at"]],
                              "side": side, "pads": pad_ids})
                continue
            names = ", ".join(label for _, _, label in on)
            viols.append(checklib.violation(
                SCRIPT, "error", v["at"],
                f"{side}.Paste", v["net"], refs,
                f"a {v['drill']:.2f} mm via hole lies under the paste of "
                f"{names}: the solder runs down the barrel and the joint "
                "voids. Move the paste windows off the via (window-pane "
                "apertures between the vias), or fill and cap it",
                SOURCE, kind="paste_on_via", side=side, pads=pad_ids))
    return viols, {"exposed_pads": eps, "vias_under_paste": hits,
                   "vias_in_paste": small}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    viols, facts = check(pcb)
    return checklib.report(SCRIPT, pcb, viols, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
