"""place_spots - free spots for parts added after routing (#852).

For each part in the spec, in order, search outward from a target point for a
spot where the part fits on a routed board: its courtyard, as the place gate
judges it, touches no other part's courtyard grown --margin, no hole grown
--margin, no track or via of another net on its side's outer copper within
--copper, no rule area that keeps footprints, pads, tracks or vias off that
copper, and no constraints.json keepout, and it stays --edge inside the board
outline (windows and notches included). Each part placed becomes an obstacle
for the next. This replaces the boards' pinned kicad/gen/find_spots.py.

The search is find_spots': rings of radius 0, step, 2*step ... out to
--max-radius, each walked from angle 0 at about one point per step, trying
the part at its angle and then at find_spots' other angle, (deg + 90) mod
180, at every point before the next ("turn": false keeps the angle). Unlike
find_spots it judges the true shapes, not bounding boxes, a rule area only
when it keeps something out, a courtyard only on the part's own side, and
it writes the footprint origin, not the courtyard centre: a part whose
origin is off its courtyard centre lands where it was judged.

Spec (JSON): {"REF": {"near": [x, y], "size"?: [w, h], "deg"?: 0,
  "side"?: "front"|"back", "nets"?: [...], "turn"?: true}, ...} in board mm.
  A part already on the board uses its own courtyard, angle, side and pad
  nets; "size" (the courtyard at deg 0, centred on the origin) is required
  for one that is not. "nets" adds nets whose tracks the part may sit on
  (its own pads' nets always are). Filled zones are not obstacles: they
  refill around the part.

Contract (SPEC section 6):
  place_spots.py --pcb B.kicad_pcb --spec spec.json [--out placements.json]
      [--ops-out ops.json] [--margin 0.25] [--copper 0.35] [--edge MM]
      [--step 0.25] [--max-radius 100] [--constraints c.json]
      [--out-report r.json]
  --out is board_update --placements' format ({"REF": {x, y, deg, side}});
  --ops-out is a place_edit ops file moving the refs already on the board.
  Payload facts: {spots: {REF: {x, y, deg, side, center, from_near_mm,
  turned, on_board}}}. exit 0; 2 = error (no spot, bad spec), nothing written.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import geom  # noqa: E402
import placelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "place_spots"
MARGIN = 0.25   # courtyard to courtyard / hole (find_spots' MARGIN)
COPPER = 0.35   # courtyard to another net's track or via (find_spots' COPPER)
STEP = 0.25
MAX_RADIUS = 100.0
# a rule area keeping any of these out of the side's copper is an obstacle
KEEPS = ("footprints", "pads", "tracks", "vias")
SPEC_KEYS = {"near", "size", "deg", "side", "nets", "turn"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def validate_spec(doc, on_board: set[str]) -> dict:
    if not isinstance(doc, dict) or not doc:
        raise CheckError("spec must be a non-empty JSON object "
                         '{"REF": {"near": [x, y], ...}}')
    for ref, s in doc.items():
        if not isinstance(s, dict):
            raise CheckError(f"spec[{ref}] must be an object")
        extra = set(s) - SPEC_KEYS
        if extra:
            raise CheckError(f"spec[{ref}]: unknown keys {sorted(extra)} "
                             f"(known: {sorted(SPEC_KEYS)})")
        near = s.get("near")
        if not (isinstance(near, list) and len(near) == 2
                and all(_num(v) for v in near)):
            raise CheckError(f"spec[{ref}].near must be [x, y] in mm")
        if "size" in s:
            sz = s["size"]
            if not (isinstance(sz, list) and len(sz) == 2
                    and all(_num(v) and v > 0 for v in sz)):
                raise CheckError(f"spec[{ref}].size must be [w, h] > 0 mm")
        elif ref not in on_board:
            raise CheckError(f"spec[{ref}]: not on the board, so it needs "
                             '"size": [w, h] (its courtyard at deg 0)')
        if "deg" in s and not _num(s["deg"]):
            raise CheckError(f"spec[{ref}].deg must be a number")
        if s.get("side", "front") not in ("front", "back"):
            raise CheckError(f"spec[{ref}].side must be front|back")
        nets = s.get("nets", [])
        if not (isinstance(nets, list) and all(isinstance(n, str)
                                               for n in nets)):
            raise CheckError(f"spec[{ref}].nets must be a list of net names")
        if not isinstance(s.get("turn", True), bool):
            raise CheckError(f"spec[{ref}].turn must be true|false")
    return doc


class Subject:
    """The part being placed: its judged shape centred on (0, 0) at deg 0,
    and the offset from that centre to its origin (local frame)."""

    def __init__(self, ref: str, s: dict, fp: placelib.Footprint | None):
        self.ref = ref
        self.near = (float(s["near"][0]), float(s["near"][1]))
        self.nets = set(s.get("nets", []))
        self.turn = s.get("turn", True)
        if fp is not None and "size" not in s:
            side = s.get("side", fp.side)
            if side != fp.side:
                fp.mirror()
            shape = fp.judge_extents_local()
            self.nets |= {p.net for p in fp.pads if p.net}
            self.deg = float(s.get("deg", fp.angle))
        else:
            w, h = s["size"]
            shape = box(-w / 2, -h / 2, w / 2, h / 2)
            side = s.get("side", fp.side if fp is not None else "front")
            self.deg = float(s.get("deg", fp.angle if fp else 0.0))
            if fp is not None:
                self.nets |= {p.net for p in fp.pads if p.net}
        self.side = side
        x0, y0, x1, y1 = shape.bounds
        self.c_local = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.local = affinity.translate(shape, -self.c_local[0],
                                        -self.c_local[1])

    def degs(self) -> list[float]:
        d = self.deg % 360.0
        return [d, (d + 90.0) % 180.0] if self.turn else [d]

    def shape_at(self, deg: float):
        """Judged shape centred on (0, 0), turned to deg (board frame)."""
        return affinity.rotate(self.local, -deg, origin=(0, 0))

    def origin(self, center, deg: float) -> tuple[float, float]:
        dx, dy = geom._rot(self.c_local[0], self.c_local[1], -deg)
        return (round(center[0] - dx, 4), round(center[1] - dy, 4))


def obstacles(model: placelib.PlaceModel, side: str, skip: set[str],
              constraints: dict, margin: float, copper: float):
    """[(shape, net or None, why)]: everything a part on `side` must miss."""
    bg = model.bg
    layer = "F.Cu" if side == "front" else "B.Cu"
    out = []
    for ref, fp in model.footprints.items():
        if ref in skip:
            continue
        if fp.side == side:
            out.append((fp.judge_extents_abs().buffer(margin), None,
                        f"{ref} courtyard"))
    seen = set()
    for p in [*bg.pads_of(), *bg.npth]:     # npth: copper-less holes too
        if p.ref in skip or p.drill is None or id(p) in seen:
            continue
        seen.add(id(p))
        hole = p.drill_poly
        if p.kind != "np_thru_hole":
            hole = hole.union(p.poly)
        if not hole.is_empty:
            out.append((hole.buffer(margin), None,
                        f"{p.ref}.{p.number or '?'} hole"))
    for t in bg.tracks_of(layer=layer):
        out.append((t.poly.buffer(copper), t.net, f"{t.net} track"))
    for v in bg.vias_of():
        if v.spans(layer):
            out.append((v.poly.buffer(copper), v.net, f"{v.net} via"))
    for ra in model.rule_areas:
        flags = ra.get("flags") or {}
        lays = ra.get("layers") or []
        if ra["outline"].is_empty or not any(
                flags.get(k) == "not_allowed" for k in KEEPS):
            continue
        if layer in lays or "*.Cu" in lays or not lays:
            out.append((ra["outline"], None,
                        f"rule area '{ra.get('name') or '?'}'"))
    for poly, why in placelib._forbidden(
            model, (constraints or {}).get("placement"), side):
        if why.startswith("rule area"):
            continue                       # taken above, with its flags
        out.append((poly, None, f"keepout '{why}'"))
    return out


def _ring(near, rad: float, step: float) -> np.ndarray:
    """find_spots' ring: from angle 0, about one point per step, rounded to
    0.01 mm as it writes them."""
    n = max(1, int(2 * math.pi * rad / step))
    a = 2 * math.pi * np.arange(n) / n
    pts = np.column_stack([near[0] + rad * np.cos(a),
                           near[1] + rad * np.sin(a)])
    return np.round(pts, 2)


def _copies(shape, centers: np.ndarray):
    """shape translated to each centre, vectorised."""
    n = len(shapely.get_coordinates(shape))
    geoms = np.full(len(centers), shape, dtype=object)
    offs = np.repeat(centers, n, axis=0)
    return shapely.transform(geoms, lambda c: c + offs)


def find_spot(sub: Subject, board, tree, obs, extra, step: float,
              max_radius: float):
    """-> (center, deg, turned) of the first free spot, or None."""
    nets = np.array([o[1] for o in obs], dtype=object)
    shapes = {d: sub.shape_at(d) for d in sub.degs()}
    rings = int(round(max_radius / step))
    for k in range(rings + 1):
        pts = _ring(sub.near, k * step, step)
        ok = {}
        for d, shp in shapes.items():
            cands = _copies(shp, pts)
            good = shapely.contains(board, cands)
            ci, oi = tree.query(cands, predicate="intersects")
            hit = np.array([n is None or n not in sub.nets
                            for n in nets[oi]], dtype=bool)
            good[ci[hit]] = False
            for e in extra:
                good &= ~shapely.intersects(cands, e)
            ok[d] = good
        for i in range(len(pts)):
            for j, d in enumerate(shapes):
                if ok[d][i]:
                    return (float(pts[i][0]), float(pts[i][1])), d, j > 0
    return None


def place(model: placelib.PlaceModel, spec: dict, constraints: dict,
          margin: float = MARGIN, copper: float = COPPER,
          edge: float | None = None, step: float = STEP,
          max_radius: float = MAX_RADIUS) -> dict:
    validate_spec(spec, set(model.footprints))
    dup = sorted(set(spec) & model.dup_refs)
    if dup:
        raise CheckError(f"refs {dup} name more than one footprint")
    if model.outline is None or model.outline.is_empty:
        raise CheckError("the board has no closed Edge.Cuts outline")
    if edge is None:
        edge = placelib.copper_edge_mm(model)
    board = model.outline.buffer(-edge, join_style="mitre")
    if board.is_empty:
        raise CheckError(f"nothing of the board is {edge} mm from its edge")
    shapely.prepare(board)
    skip = set(spec)
    built, spots, placed = {}, {}, {"front": [], "back": []}
    for ref, s in spec.items():
        sub = Subject(ref, s, model.footprints.get(ref))
        if sub.side not in built:
            obs = obstacles(model, sub.side, skip, constraints, margin,
                            copper)
            built[sub.side] = (shapely.STRtree([o[0] for o in obs]), obs)
        tree, obs = built[sub.side]
        got = find_spot(sub, board, tree, obs, placed[sub.side], step,
                        max_radius)
        if got is None:
            raise CheckError(
                f"{ref}: no free spot within {max_radius} mm of "
                f"{list(sub.near)} on the {sub.side} (margin {margin}, "
                f"copper {copper}, edge {edge} mm)")
        center, deg, turned = got
        shp = affinity.translate(sub.shape_at(deg), *center)
        placed[sub.side].append(shp.buffer(margin))
        x, y = sub.origin(center, deg)
        spots[ref] = {"x": x, "y": y, "deg": deg, "side": sub.side,
                      "center": [center[0], center[1]],
                      "from_near_mm": round(math.dist(center, sub.near), 3),
                      "turned": turned,
                      "on_board": ref in model.footprints}
    return spots


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", default=None,
                    help="placements JSON for board_update --placements")
    ap.add_argument("--ops-out", default=None,
                    help="place_edit ops moving the refs already placed")
    ap.add_argument("--margin", type=float, default=MARGIN)
    ap.add_argument("--copper", type=float, default=COPPER)
    ap.add_argument("--edge", type=float, default=None,
                    help="courtyard to board edge, mm (default: the "
                         "board's copper-to-edge rule)")
    ap.add_argument("--step", type=float, default=STEP)
    ap.add_argument("--max-radius", type=float, default=MAX_RADIUS)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (default: next to the board)")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)
    for k in ("margin", "copper", "step", "max_radius"):
        v = getattr(args, k)
        if not math.isfinite(v) or v < 0 or (k == "step" and v == 0):
            raise CheckError(f"--{k.replace('_', '-')} must be a number "
                             f"{'> 0' if k == 'step' else '>= 0'}")
    if args.edge is not None and not (math.isfinite(args.edge)
                                      and args.edge >= 0):
        raise CheckError("--edge must be a number >= 0")

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    spec = checklib.load_json(args.spec, "spec")
    cons = Path(args.constraints) if args.constraints else \
        pcb.parent / "constraints.json"
    constraints = checklib.load_json(cons, "constraints") \
        if cons.is_file() else {}
    model = placelib.PlaceModel(pcb)
    spots = place(model, spec, constraints, args.margin, args.copper,
                  args.edge, args.step, args.max_radius)
    if args.out:
        Path(args.out).write_text(json.dumps(
            {r: {k: v[k] for k in ("x", "y", "deg", "side")}
             for r, v in spots.items()}, indent=1) + "\n")
    if args.ops_out:
        ops = [{"op": "place", "ref": r, "x": v["x"], "y": v["y"],
                "deg": v["deg"], "side": v["side"]}
               for r, v in spots.items() if v["on_board"]]
        Path(args.ops_out).write_text(json.dumps(
            {"version": 1, "ops": ops}, indent=1) + "\n")
    payload = {"script": SCRIPT, "status": "pass", "board": str(pcb),
               "facts": {"spots": spots, "out": args.out,
                         "ops_out": args.ops_out}}
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
