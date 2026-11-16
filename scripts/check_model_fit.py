"""check_model_fit.py - every footprint's 3D model sits on its own pads
(P8 verify suite).

The hit: EasyEDA's model of the HRO TYPE-C-31-M-12 USB-C (lite-main J1, as
lib_pull fetches it) sits 2.2 mm off its footprint: its shell legs and
locating pegs land beside the holes, its body hangs over the board edge the
wrong way, and the render, the STEP export and the enclosure fitted to that
STEP (hive-mech reads part heights and port positions from it) all show the
connector where it isn't. No gate looked: the model is not copper. fpfix's
body_flags read a pulled library footprint's VRML once, THT pins only, and
never the board.

The model is read as geometry (lib/modelgeom.py: VRML faces, STEP B-rep
faces) and placed by the footprint's own (model (offset) (scale) (rotate)),
in the footprint's frame. Its contact features are then laid against the
pads:
  leads  geometry in the seating band (z -0.1..+0.2 mm) that isn't the
         body: flat faces merged level by level into regions, and a region
         (or any other face) bigger than LEAD_AREA_X pads or longer than
         LEAD_SPAN_X pads is the body's underside, not a lead;
  pins   geometry wholly below the board (z < -0.5 mm): pins, shell legs and
         locating pegs, scored only at each hole's centre (HOLE_CORE), so a
         fat peg in a pin's hole is no better fit than the pin.
The fit is the pads' worth the features cover (each SMD pad's covered share,
each hole's centre), worked out for every slide of the model within
+-SEARCH_MM on a RES_MM raster at once, by cross-correlating the feature
raster with the pad weights; at the model's own turn and at +90/180/270 deg.
When some placement covers more than the model as placed, by GAIN_MIN pads
and GAIN_REL of its own score, and meets (MET_FRAC) no fewer pads, the model
is off, and the finding gives the (offset)/(rotate) of that placement (the
middle of its plateau nearest the model as placed). Symmetric parts can't
show a 180 deg turn (or a 90 deg one if square): only their marking differs.
Findings, one per footprint per kind, kind:
  model_offset       error: a slide of >= MIN_SHIFT_MM fits better
  model_rotation     error: a turn (and slide) fits better
  model_scale        error: (scale) is not 1 1 1 (a model is drawn to size)
  model_floats       error: the model's lowest point is above the board
                     (z > FLOAT_MM): its leads don't touch their pads
  model_in_board     error: the model reaches into the board's material
                     (z < -SINK_MM, above its bottom face) inside the
                     outline where the footprint has no hole: a Z offset
                     gone wrong, or a peg or mid-mount body with nowhere to
                     go. An edge-mount part's legs straddling the board
                     edge are outside the outline, so they pass.
  model_unmatched    warning: under half its pads are met and no slide or
                     turn fits better: a wrong model, or one tilted on X/Y
  model_mount_height error: the part's stated mounting height (constraints
                     mechanical.mount_heights, #980/#1191) and its model's
                     disagree by more than tol_mm. The model is the part as
                     the render, the STEP and the enclosure see it, so a
                     standoff drawn at the wrong height there is a wrong
                     part height everywhere. Datum body_underside: the
                     lowest flat level of the model's faces above the
                     seating band whose merged area is at least
                     MOUNT_AREA_FRAC of the model's footprint box (a lead's
                     shoulder is no underside); lead_protrusion: how far
                     the model reaches below the board's bottom face. A
                     part with no entry is not judged; one whose model
                     can't be read or shows no such face is listed in
                     facts mount_heights_not_judged. The check measures the
                     model against the stated figure only: a stated figure
                     that is itself wrong passes.
A model with no file on disk is check_model_paths' / render's concern, and a
hidden one is never drawn: both are skipped (counted in the report's facts).

The repair belongs in the footprint library (fix the (model) there and
update the board from it, so the next update doesn't bring the old one
back); `fit` in each finding is the (offset)/(rotate) to write.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

import sexpdata
from shapely import affinity
from shapely.geometry import box

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import kc  # noqa: E402
import modelgeom  # noqa: E402
from geom import _kid, _kids, _nums, _strs, _tok, fp_field  # noqa: E402

SCRIPT = "check_model_fit"
SOURCE = SCRIPT
SEAT_Z = (-0.1, 0.2)     # mm: the seating band a lead's foot lies in
BELOW_Z = -0.5           # mm: wholly below this is a pin or peg in a hole
FEATURE_PAD = 0.02       # mm: features grow this much (a face edge-on to a pad)
LEAD_AREA_X = 2.0        # a seating face bigger than this x the largest pad is body
LEAD_SPAN_X = 1.5        # ... or longer than this x the longest pad side
SEARCH_MM = 5.0          # mm: the farthest slide tried
RES_MM = 0.05            # the raster's pixel
MET_FRAC = 0.05          # a pad this much covered by a feature is met
FLAT_MM = 0.01           # a face this flat in z is horizontal
HOLE_CORE = 0.25         # a hole scores over this share of its width
PLATEAU = 0.02           # pads' worth: scores this close tie
GAIN_MIN = 0.3           # pads' worth a slide or turn must win by ...
GAIN_REL = 0.15          # ... and this share of its own score
TURN_PENALTY_MM = 0.2    # mm: a turn must beat a plain slide by this much
MIN_SHIFT_MM = 0.2       # mm: a shorter slide is no finding
FLOAT_MM = 0.15          # mm: a model this far above the board floats
SINK_MM = 0.3            # mm: a model detail this deep in the board is noise
HOLE_SLACK_MM = 0.3      # a pin's corner may stand this far past its drill
UNIT_EPS = 1e-3
MOUNT_AREA_FRAC = 0.2    # a body underside covers this share of the xy box
MOUNT_DATUMS = ("body_underside", "lead_protrusion")


def _sval(x) -> str:
    return x.value() if isinstance(x, sexpdata.Symbol) else str(x)


def stock_dirs() -> list[Path]:
    """Where ${KICAD<n>_3DMODEL_DIR} points when the environment doesn't say:
    beside the pinned kicad-cli (HWDE_KICAD_CLI), the system's KiCad, then
    each KiCad install found (env.kicad_installs, the Hive's AppDir under
    the shared tools folder among them)."""
    out = []
    try:
        import env
        pin, _ = env.skill_env("KICAD_CLI")
        if pin:
            out += [r / "share" / "kicad" / "3dmodels"
                    for r in Path(pin).resolve().parents]
        out.append(Path("/usr/share/kicad/3dmodels"))
        out += [r / "share" / "kicad" / "3dmodels" for r in env.kicad_installs()]
    except Exception:  # noqa: BLE001 - a lookup that fails finds nothing
        pass
    return [d for d in out if d.is_dir()]


def resolver(pcb: Path):
    envv = dict(os.environ)
    stock = stock_dirs()
    if stock:
        for n in ("", "6", "7", "8", "9", "10", "11"):
            envv.setdefault(f"KICAD{n}_3DMODEL_DIR", str(stock[0]))
    return kc._model_resolver(pcb, envv)


# ------------------------------------------------------------ the board

def _xyz(node, key, dflt):
    k = _kid(node, key)
    x = _kid(k, "xyz") if k is not None else None
    v = _nums(x) if x is not None else []
    return tuple(v[:3]) if len(v) >= 3 else dflt


def _hidden(node) -> bool:
    h = _kid(node, "hide")
    if h is not None:
        return len(h) < 2 or _tok(h[1]) != "no"
    return any(not isinstance(t, list) and _tok(t) == "hide" for t in node[2:])


def _pads(fp, angle: float) -> list[tuple[str, bool, object]]:
    """(number, through, copper or hole polygon in the footprint's file
    frame) of every pad with copper or a hole. An SMD pad with neither a
    paste nor a mask opening is bare copper under the mask, a Kelvin
    footprint's sense tab (R0805_Kelvin pads 3 and 4, #612) or a net tie's
    bridge: no lead lands on it, so the model is not fitted to it."""
    out = []
    for pad in _kids(fp, "pad"):
        ps = _strs(pad)
        number = ps[0] if ps else ""
        toks = [_tok(t) for t in pad[1:4] if not isinstance(t, list)]
        through = any(t in ("thru_hole", "np_thru_hole") for t in toks)
        layers = " ".join(_strs(_kid(pad, "layers")) if _kid(pad, "layers")
                          is not None else [])
        if not through and ".Cu" not in layers:
            continue                       # a paste or mask aperture
        if not through and ".Paste" not in layers and ".Mask" not in layers:
            continue                       # bare copper: no lead lands on it
        at = _nums(_kid(pad, "at")) if _kid(pad, "at") is not None else [0, 0]
        if through and _kid(pad, "drill") is not None:
            d = _nums(_kid(pad, "drill"))
            w, h = (d[0], d[1]) if len(d) >= 2 else ((d or [0])[0],) * 2
        else:
            sz = _nums(_kid(pad, "size")) if _kid(pad, "size") is not None else []
            w, h = (sz[0], sz[1]) if len(sz) >= 2 else (0.0, 0.0)
        if w <= 0 or h <= 0:
            continue
        rel = (at[2] if len(at) > 2 else 0.0) - angle
        poly = affinity.rotate(box(-w / 2, -h / 2, w / 2, h / 2), -rel,
                               origin=(0, 0))
        out.append((number, through, affinity.translate(poly, at[0], at[1])))
    return out


def _models(fp) -> list[tuple]:
    """(path, offset, scale, rotate, hidden) of each of a footprint's
    (model ...) nodes, in order."""
    models = []
    for m in _kids(fp, "model"):
        if len(m) < 2:
            continue
        models.append((_sval(m[1]), _xyz(m, "offset", (0.0, 0.0, 0.0)),
                       _xyz(m, "scale", (1.0, 1.0, 1.0)),
                       _xyz(m, "rotate", (0.0, 0.0, 0.0)), _hidden(m)))
    return models


def footprints(pcb: Path):
    """(ref, (x, y), angle, back, pads, [(path, offset, scale, rotate)])
    for every footprint with a shown model."""
    tree = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    for fp in _kids(tree, "footprint"):
        ref = fp_field(fp, "Reference", "?")
        at = _nums(_kid(fp, "at")) if _kid(fp, "at") is not None else [0, 0]
        angle = at[2] if len(at) > 2 else 0.0
        layer = _kid(fp, "layer")
        back = bool(layer is not None and _strs(layer)
                    and _strs(layer)[0].startswith("B."))
        models = _models(fp)
        if models:
            yield ref, (at[0], at[1]), angle, back, _pads(fp, angle), models


# ------------------------------------------------------------ the fit

def _features(faces, pads):
    """(lead boxes, pin boxes) as (x0, y0, x1, y1): the model's contact
    features (module doc). Flat faces in the seating band are merged level
    by level into regions first: EasyEDA cuts a body's underside into
    triangles, each smaller than a pad, and only the whole region shows it
    is the body: its box bigger than LEAD_AREA_X pads or longer than
    LEAD_SPAN_X pads (the thin ring of a body's rounded bottom edge)."""
    from shapely.geometry import MultiPoint
    from shapely.ops import unary_union
    smd = [p for _, t, p in pads if not t]
    big = max((p.area for p in smd), default=1.0) * LEAD_AREA_X
    span = max((max(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1])
                for p in smd), default=1.0) * LEAD_SPAN_X

    def lead(b) -> bool:
        w, h = b[2] - b[0], b[3] - b[1]
        return w * h <= big and max(w, h) <= span

    leads, pins = [], []
    flat: dict[float, list] = {}
    for face in faces:
        zs = [p[2] for p in face]
        xs = [p[0] for p in face]
        ys = [p[1] for p in face]
        b = (min(xs) - FEATURE_PAD, min(ys) - FEATURE_PAD,
             max(xs) + FEATURE_PAD, max(ys) + FEATURE_PAD)
        if max(zs) < BELOW_Z:
            pins.append(b)
        elif SEAT_Z[0] <= min(zs) and max(zs) <= SEAT_Z[1]:
            if len(face) >= 3 and max(zs) - min(zs) < FLAT_MM:
                flat.setdefault(round(min(zs) / FLAT_MM), []).append(
                    MultiPoint([(x, y) for x, y, _ in face]).convex_hull
                    .buffer(FEATURE_PAD, join_style=2))
            elif lead(b):
                leads.append(b)
    for polys in flat.values():
        merged = unary_union(polys)
        for g in getattr(merged, "geoms", [merged]):
            if g.area > 0 and lead(g.bounds):
                leads.append(g.bounds)
    return leads, pins


class _Frame:
    """The raster the pads are drawn on: RES_MM pixels over the pads' box,
    and the same box grown by the search radius for the model's features.
    Pad p weighs 1/(its pixel count), so a pad the features cover whole
    scores 1 and the score of a placement is in pads' worth."""

    def __init__(self, pads):
        import numpy as np
        import shapely
        self.n = int(round(SEARCH_MM / RES_MM))
        x0 = min(p.bounds[0] for _, _, p in pads)
        y0 = min(p.bounds[1] for _, _, p in pads)
        x1 = max(p.bounds[2] for _, _, p in pads)
        y1 = max(p.bounds[3] for _, _, p in pads)
        self.ox = math.floor(x0 / RES_MM) * RES_MM
        self.oy = math.floor(y0 / RES_MM) * RES_MM
        self.w = int(math.ceil((x1 - self.ox) / RES_MM)) + 1
        self.h = int(math.ceil((y1 - self.oy) / RES_MM)) + 1
        xs = self.ox + (np.arange(self.w) + 0.5) * RES_MM
        ys = self.oy + (np.arange(self.h) + 0.5) * RES_MM
        gx, gy = np.meshgrid(xs, ys)
        self.weights = {False: np.zeros((self.h, self.w)),
                        True: np.zeros((self.h, self.w))}
        self.masks = []          # (number, through, row idx, col idx)
        for number, through, poly in pads:
            if through:      # a pin fits its hole or not: score its centre
                c = poly.centroid
                minx, miny, maxx, maxy = poly.bounds
                poly = c.buffer(max(RES_MM, HOLE_CORE * min(maxx - minx,
                                                            maxy - miny) / 2))
            m = shapely.contains_xy(poly, gx, gy)
            if not m.any():      # a pad under one pixel: its centre pixel
                c = poly.centroid
                m[min(self.h - 1, int((c.y - self.oy) / RES_MM)),
                  min(self.w - 1, int((c.x - self.ox) / RES_MM))] = True
            rows, cols = np.nonzero(m)
            self.weights[through][rows, cols] += 1.0 / len(rows)
            self.masks.append((number or "(unnumbered)", through, rows, cols))

    def raster(self, boxes):
        """The boxes drawn on the grown raster (n pixels more each side)."""
        import numpy as np
        n = self.n
        u = np.zeros((self.h + 2 * n, self.w + 2 * n), dtype=float)
        gx0 = self.ox - n * RES_MM
        gy0 = self.oy - n * RES_MM
        for bx0, by0, bx1, by1 in boxes:
            j0 = max(0, int(math.floor((bx0 - gx0) / RES_MM)))
            j1 = min(u.shape[1], max(j0 + 1, int(math.ceil((bx1 - gx0) / RES_MM))))
            i0 = max(0, int(math.floor((by0 - gy0) / RES_MM)))
            i1 = min(u.shape[0], max(i0 + 1, int(math.ceil((by1 - gy0) / RES_MM))))
            if j0 < u.shape[1] and i0 < u.shape[0] and j1 > 0 and i1 > 0:
                u[i0:i1, j0:j1] = 1.0
        return u


class _Fit:
    """How well a placed model's features cover its pads, for every slide
    of the model on the RES_MM grid within +-SEARCH_MM, by one
    cross-correlation of the feature raster with the pad weights."""

    def __init__(self, faces, pads, frame: _Frame):
        from scipy.signal import correlate
        leads, pins = _features(faces, pads)
        self.frame = frame
        self.u = {False: frame.raster(leads), True: frame.raster(pins)}
        self.usable = any((leads if not t else pins) for _, t, _ in pads)
        score = None
        for kind in (False, True):
            if not frame.weights[kind].any() or not self.u[kind].any():
                continue
            c = correlate(self.u[kind], frame.weights[kind], mode="valid",
                          method="fft")
            score = c if score is None else score + c
        n = frame.n
        self.score = score if score is not None else \
            __import__("numpy").zeros((2 * n + 1, 2 * n + 1))

    def at(self, i: int, j: int) -> float:
        """The score with the model slid (j, i) pixels (x, y)."""
        n = self.frame.n
        return float(self.score[n - i, n - j])

    def met(self, i: int = 0, j: int = 0) -> list[str]:
        """The pads a feature covers at least MET_FRAC of, slid (j, i)."""
        n = self.frame.n
        hit = []
        for number, through, rows, cols in self.frame.masks:
            cov = self.u[through][rows + n - i, cols + n - j].mean()
            if cov >= MET_FRAC:
                hit.append(number)
        return hit

    def best(self):
        """(score, i, j): the best slide, in pixels; on a plateau (within
        PLATEAU of the top) the middle of the part of it nearest no slide."""
        import numpy as np
        n = self.frame.n
        top = float(self.score.max())
        on = self.score >= top - PLATEAU - 1e-9
        cells = {(n - a, n - b) for a, b in zip(*np.nonzero(on))}
        start = min(cells, key=lambda c: (c[0] ** 2 + c[1] ** 2, c))
        comp, todo = {start}, [start]
        while todo:
            i, j = todo.pop()
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    c = (i + di, j + dj)
                    if c not in comp and c in cells:
                        comp.add(c)
                        todo.append(c)
        mi = sum(c[0] for c in comp) / len(comp)
        mj = sum(c[1] for c in comp) / len(comp)
        # the middle of a bent plateau may be off it: then its nearest cell
        mid = min(comp, key=lambda c: ((c[0] - mi) ** 2 + (c[1] - mj) ** 2, c))
        if abs(mid[0] - mi) <= 1 and abs(mid[1] - mj) <= 1:
            return self.at(*mid), mi, mj
        return self.at(*mid), mid[0], mid[1]


def _r(v: float, nd: int = 3) -> float:
    return round(v, nd) + 0.0   # never -0.0


def _better(a, b) -> bool:
    """Placement a = (score, turn, i, j, ...) beats b: a higher score by
    more than PLATEAU, else the shorter slide, a turn costing
    TURN_PENALTY_MM."""
    def cost(c):
        return math.hypot(c[2], c[3]) * RES_MM + (TURN_PENALTY_MM if c[1] else 0.0)
    if abs(a[0] - b[0]) > PLATEAU:
        return a[0] > b[0]
    return cost(a) < cost(b) - 1e-9


def fit_model(mesh, offset, scale, rotate, back, pads) -> list[dict]:
    """The findings (kind, severity, detail facts) for one placed model."""
    out = []
    if any(abs(s - 1.0) > UNIT_EPS for s in scale):
        out.append({"kind": "model_scale", "severity": "error",
                    "scale": list(scale),
                    "msg": f"(scale {' '.join(f'{s:g}' for s in scale)}) "
                           "stretches the model; a part's model is drawn to "
                           "size, so its scale is 1 1 1",
                    "fit": {"scale": [1, 1, 1]}})
    bx = modelgeom.placed_box(mesh, offset, scale, rotate, back)
    if bx[2] > FLOAT_MM:
        out.append({"kind": "model_floats", "severity": "error",
                    "z_min": _r(bx[2]),
                    "msg": f"the model's lowest point is {bx[2]:.2f} mm above "
                           "the board: it floats off its pads",
                    "fit": {"offset": [_r(offset[0]), _r(offset[1]),
                                       _r(offset[2] - bx[2])]}})
    if not pads:
        return out
    frame = _Frame(pads)
    fit = _Fit(modelgeom.place(mesh, offset, scale, rotate, back), pads, frame)
    if not fit.usable:
        return out
    s0, met0 = max(0.0, fit.at(0, 0)), fit.met()
    best = None   # (score, turn, i, j, fit)
    for turn in (0, 90, 180, 270):
        rot = (rotate[0], rotate[1], (rotate[2] + turn) % 360)
        f = fit if not turn else _Fit(
            modelgeom.place(mesh, offset, scale, rot, back), pads, frame)
        sc, i, j = f.best()
        cand = (sc, turn, i, j, f)
        if best is None or _better(cand, best):
            best = cand
    gain = best[0] - s0
    if gain < max(GAIN_MIN, GAIN_REL * best[0]) or (
            not best[1] and math.hypot(best[2], best[3]) * RES_MM
            < MIN_SHIFT_MM - 1e-9):
        best = None
    unmet = [n or "(unnumbered)" for n, _, _ in pads]
    for n in met0:
        unmet.remove(n)
    if best is None:
        if 2 * len(met0) < len(pads):
            out.append({"kind": "model_unmatched", "severity": "warning",
                        "met": len(met0), "pads": len(pads),
                        "unmet_pads": sorted(set(unmet)),
                        "msg": f"the model's leads and pins meet {len(met0)} of "
                               f"{len(pads)} pads and no shift or turn does "
                               "better: a wrong model, or one tilted on X/Y"})
        return out
    score, turn, i, j, f = best
    dx, dy = j * RES_MM, i * RES_MM
    count = len(f.met(round(i), round(j)))
    if count < len(met0):
        return out   # it covers some pads better by leaving others
    # the slide is in the file frame (Y down); the offset is in the model's
    # Y-up frame, which a back-side part's mirrored locals flip again
    new_off = [_r(offset[0] + dx, 2), _r(offset[1] + (dy if back else -dy), 2),
               _r(offset[2])]
    new_rot = [_r(rotate[0]), _r(rotate[1]), _r((rotate[2] + turn) % 360)]
    where = f"slid {math.hypot(dx, dy):.2f} mm (dx {dx:+.2f}, dy {dy:+.2f})"
    if turn:
        kind = "model_rotation"
        how = f"turned {turn} deg" + (f" and {where}" if (dx or dy) else "")
    else:
        kind, how = "model_offset", where
    out.append({"kind": kind, "severity": "error",
                "met": len(met0), "met_fit": count, "pads": len(pads),
                "cover": _r(s0), "cover_fit": _r(score),
                "unmet_pads": sorted(set(unmet)),
                "shift_mm": [_r(dx, 2), _r(dy, 2)], "turn_deg": turn,
                "msg": f"the model's leads and pins cover {s0:.2f} pads' worth "
                       f"of its {len(pads)} pads ({len(met0)} met) as placed; "
                       f"{how} they cover {score:.2f} ({count} met)",
                "fit": {"offset": new_off, "rotate": new_rot}})
    return out


def in_board(mesh, offset, scale, rotate, back, pads, region,
             thickness: float) -> dict | None:
    """The finding when the placed model reaches into the board's material
    (z between -SINK_MM and SINK_MM - thickness) inside `region` (the
    board, in the footprint's file frame) and outside every hole of the
    footprint, else None."""
    import numpy as np
    import shapely
    from shapely.ops import unary_union
    holes = unary_union([p for _, t, p in pads if t]).buffer(HOLE_SLACK_MM) \
        if any(t for _, t, _ in pads) else None
    f = modelgeom.transform(offset, scale, rotate, back)
    pts = np.array([q for face in mesh.faces for q in map(f, face)
                    if SINK_MM - thickness < q[2] < -SINK_MM])
    if not len(pts):
        return None
    inside = shapely.contains_xy(region, pts[:, 0], pts[:, 1])
    if holes is not None:
        inside &= ~shapely.contains_xy(holes, pts[:, 0], pts[:, 1])
    if not inside.any():
        return None
    hit = pts[inside]
    depth = float(-hit[:, 2].min())
    x, y = float(hit[:, 0].mean()), float(hit[:, 1].mean())
    return {"kind": "model_in_board", "severity": "error",
            "depth_mm": _r(depth), "at_local": [_r(x), _r(y)],
            "msg": f"the model reaches {depth:.2f} mm into the board around "
                   f"({x:.2f}, {y:.2f}) in the footprint, where the footprint "
                   "has no hole: its Z offset is wrong, or the part needs a "
                   "hole or cut-out there (a peg, a mid-mount body)"}


class _Board:
    """The board's outline (inner windows cut out) and thickness, read only
    when a model reaches below the board."""

    def __init__(self, pcb: Path):
        self.pcb = pcb
        self._geo = None

    def _load(self):
        if self._geo is None:
            import geom
            from shapely.ops import unary_union
            bg = geom.load_board(self.pcb)
            outline = bg.outline
            windows = [f for f in getattr(bg, "outline_faces", [])[1:]
                       if outline.contains(f.representative_point())]
            if windows:
                outline = outline.difference(unary_union(windows))
            self._geo = (outline.buffer(-0.05), float(bg.thickness or 1.6))
        return self._geo

    @property
    def thickness(self) -> float:
        return self._load()[1]

    def region(self, pos, angle: float):
        """The board in a footprint's file frame (placelib: abs = pos +
        R(-angle) local, so local = R(angle)(abs - pos))."""
        outline = self._load()[0]
        return affinity.rotate(affinity.translate(outline, -pos[0], -pos[1]),
                               angle, origin=(0, 0))


# ------------------------------------------------------------ the library fit

# the findings a library footprint's (model ...) is refitted for; the rest
# (model_unmatched: a wrong model) are the part's drawing's to settle
REFIT = ("model_offset", "model_rotation", "model_scale", "model_floats")
REFIT_PASSES = 3        # a scale or turn moves the floor: fit again after it
_XYZ_KEY = r"\(\s*{0}\s*\(\s*xyz\s+[^()]*\)\s*\)"


def lib_model_file(path: str, fp_file: Path) -> Path | None:
    """The file a library footprint's model path names: the path itself
    when it is one (easyeda2kicad writes absolute paths), else the file of
    that name in a *.3dshapes beside the footprint's .pretty (a
    ${KIPRJMOD}/.. path, or a checkout that moved)."""
    q = Path(path)
    if "$" not in path and q.is_absolute() and q.is_file():
        return q
    return kc.model_lib_file(path, sorted(Path(fp_file).resolve().parent
                                          .parent.glob("*.3dshapes")))


def refit(mesh, offset, scale, rotate, pads):
    """(offset, scale, rotate, kinds fixed, findings left) for a library
    footprint's model: the fits fit_model gives for REFIT findings applied
    until none is left (or REFIT_PASSES), and given up (the model as it
    was) when a slide or turn is still found after them."""
    xf, kinds = (tuple(offset), tuple(scale), tuple(rotate)), []
    for _ in range(REFIT_PASSES):
        found = fit_model(mesh, *xf, False, pads)
        fix = [h for h in found if h["kind"] in REFIT and h.get("fit")]
        if not fix:
            return (*xf, kinds, found)
        off, sc, rot = map(list, xf)
        for h in fix:
            kinds.append(h["kind"])
            f = h["fit"]
            if h["kind"] == "model_floats":
                off[2] = f["offset"][2]
            if "scale" in f:
                sc = list(f["scale"])
            if h["kind"] in ("model_offset", "model_rotation"):
                off[:2], rot = f["offset"][:2], list(f["rotate"])
        xf = tuple(tuple(float(v) for v in t) for t in (off, sc, rot))
    found = fit_model(mesh, *xf, False, pads)
    if any(h["kind"] in REFIT for h in found):
        return (tuple(offset), tuple(scale), tuple(rotate), [],
                found + [{"kind": "model_unsettled", "severity": "warning",
                          "msg": "the fit kept moving; the model is left as "
                                 "it was"}])
    return (*xf, kinds, found)


def _xyz_text(node: str, key: str, v) -> str:
    """The (model ...) node text with its (key (xyz ...)) set to v."""
    put = f"({key} (xyz {' '.join(f'{_r(c, 4):g}' for c in v)}))"
    pat = re.compile(_XYZ_KEY.format(key))
    if pat.search(node):
        return pat.sub(lambda _m: put, node, count=1)
    end = node.rstrip().rfind(")")
    return node[:end].rstrip() + " " + put + node[end:]


def fit_footprint(fp_file: Path, dry_run: bool = False) -> dict:
    """Fit each shown 3D model of a library footprint (.kicad_mod, either
    form) to the footprint's own pads, rewriting its (offset) (scale)
    (rotate) in place: lib_pull runs this on every part it imports, so a
    model EasyEDA misplaced never reaches a board. Text surgery on the
    (model ...) nodes only; a second run changes nothing."""
    import fpfix
    fp_file = Path(fp_file)
    text = fp_file.read_text(encoding="utf-8")
    fp = sexpdata.loads(text)
    pads = _pads(fp, 0.0)
    spans = [(s, e) for h, s, e in fpfix.top_level_nodes(text) if h == "model"]
    rows, edits = [], []
    for (path, offset, scale, rotate, hidden), span in zip(_models(fp), spans):
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        row = {"model": name}
        rows.append(row)
        node = text[span[0]:span[1]]
        if hidden:
            row["skipped"] = "hidden"
            continue
        if re.search(r"\(\s*at\s*\(\s*xyz", node):
            row["skipped"] = "(at) offset in inches: KiCad 5 form"
            continue
        f = lib_model_file(path, fp_file)
        if f is None:
            row["skipped"] = "no model file"
            continue
        try:
            mesh = modelgeom.load(f)
        except (OSError, ValueError) as exc:
            row["skipped"] = f"unreadable: {type(exc).__name__}: {exc}"
            continue
        if mesh is None:
            row["skipped"] = "no geometry"
            continue
        off, sc, rot, kinds, left = refit(mesh, offset, scale, rotate, pads)
        if left:
            row["left"] = [{"kind": h["kind"], "severity": h["severity"],
                            "msg": h["msg"]} for h in left]
        if not kinds:
            continue
        row["fixed"] = sorted(set(kinds))
        row["from"] = {"offset": list(offset), "scale": list(scale),
                       "rotate": list(rotate)}
        row["to"] = {"offset": list(off), "scale": list(sc),
                     "rotate": list(rot)}
        for key, v in (("offset", off), ("scale", sc), ("rotate", rot)):
            node = _xyz_text(node, key, v)
        edits.append((span, node))
    if edits and not dry_run:
        for (s, e), node in sorted(edits, reverse=True):
            text = text[:s] + node + text[e:]
        fp_file.write_text(text, encoding="utf-8")
    return {"footprint": fp_file.stem, "file": str(fp_file),
            "changed": bool(edits), "models": rows}


def fit_lib(pretty: Path, names: list[str] | None = None,
            dry_run: bool = False) -> dict:
    """fit_footprint over a .pretty (only `names` when given)."""
    import fpfix
    files = sorted(Path(pretty).glob("*.kicad_mod"))
    if names is not None:
        want = {fpfix._fp_stem(n) for n in names}
        files = [f for f in files if fpfix._fp_stem(f.name) in want]
    rows = []
    for f in files:
        try:
            rows.append(fit_footprint(f, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - one bad file fits nothing
            rows.append({"footprint": f.stem, "file": str(f), "changed": False,
                         "error": f"{type(exc).__name__}: {exc}"})
    return {"footprints": len(rows),
            "changed": sum(1 for r in rows if r["changed"]),
            "fixed": [{"footprint": r["footprint"], **m} for r in rows
                      for m in r.get("models", []) if m.get("fixed")],
            "left": [{"footprint": r["footprint"], "model": m["model"],
                      "kind": h["kind"], "msg": h["msg"]} for r in rows
                     for m in r.get("models", []) for h in m.get("left", [])],
            "errors": {r["footprint"]: r["error"] for r in rows
                       if r.get("error")}}


# ------------------------------------------------------------ mount heights

def mount_heights(constraints: dict | None) -> dict[str, dict]:
    """constraints mechanical.mount_heights, checked: {ref: {height_mm,
    datum, tol_mm, source[, model]}}. A malformed entry is a CheckError."""
    mech = (constraints or {}).get("mechanical") or {}
    raw = mech.get("mount_heights") or {}
    if not isinstance(raw, dict):
        raise checklib.CheckError("mechanical.mount_heights must be a mapping "
                                  "{ref: {height_mm, datum, tol_mm, source}}")
    out = {}
    for ref, e in raw.items():
        if not isinstance(e, dict):
            raise checklib.CheckError(f"mount_heights.{ref}: not a mapping")
        try:
            h, tol = float(e["height_mm"]), float(e.get("tol_mm", 0.3))
        except (KeyError, TypeError, ValueError):
            raise checklib.CheckError(
                f"mount_heights.{ref}: height_mm (and tol_mm) must be numbers")
        datum = e.get("datum", "body_underside")
        if datum not in MOUNT_DATUMS:
            raise checklib.CheckError(f"mount_heights.{ref}: datum {datum!r} "
                                      f"is not one of {', '.join(MOUNT_DATUMS)}")
        if tol < 0 or not str(e.get("source") or "").strip():
            raise checklib.CheckError(f"mount_heights.{ref}: tol_mm must be "
                                      ">= 0 and source (the drawing) given")
        out[str(ref)] = {"height_mm": h, "datum": datum, "tol_mm": tol,
                         "source": str(e["source"]),
                         "model": str(e.get("model") or "")}
    return out


def body_underside(faces) -> float | None:
    """The lowest flat level above the seating band covering MOUNT_AREA_FRAC
    of the model's xy box (merged level by level), or None."""
    from shapely.geometry import MultiPoint
    from shapely.ops import unary_union
    if not faces:
        return None
    xs = [p[0] for f in faces for p in f]
    ys = [p[1] for f in faces for p in f]
    need = (max(xs) - min(xs)) * (max(ys) - min(ys)) * MOUNT_AREA_FRAC
    flat: dict[int, list] = {}
    for face in faces:
        zs = [p[2] for p in face]
        if len(face) >= 3 and max(zs) - min(zs) < FLAT_MM \
                and min(zs) > SEAT_Z[1]:
            flat.setdefault(round(min(zs) / FLAT_MM), []).append(
                MultiPoint([(x, y) for x, y, _ in face]).convex_hull)
    for lvl in sorted(flat):
        if need > 0 and unary_union(flat[lvl]).area >= need:
            return lvl * FLAT_MM
    return None


def judge_mount(entry: dict, mesh, offset, scale, rotate, back,
                thickness) -> tuple[dict | None, str | None]:
    """(finding or None, why not judged or None) for one stated height."""
    faces = modelgeom.place(mesh, offset, scale, rotate, back)
    if entry["datum"] == "body_underside":
        got = body_underside(faces)
        if got is None:
            return None, "the model shows no flat body underside"
    else:
        zmin = min((p[2] for f in faces for p in f), default=0.0)
        got = -zmin - thickness()
    want, tol = entry["height_mm"], entry["tol_mm"]
    if abs(got - want) <= tol + UNIT_EPS:
        return None, None
    what = "body underside" if entry["datum"] == "body_underside" else \
        "lead protrusion below the board"
    return {"kind": "model_mount_height", "severity": "error",
            "msg": f"its {what} is {got:.2f} mm, the part is stated at "
                   f"{want:g} mm +-{tol:g} ({entry['source']})",
            "measured_mm": _r(got, 2), "height_mm": want,
            "datum": entry["datum"], "tol_mm": tol,
            "height_source": entry["source"]}, None


# ------------------------------------------------------------ the check

def check(pcb: Path, constraints: dict | None = None
          ) -> tuple[list[dict], dict]:
    resolve = resolver(pcb)
    facts = {"models": 0, "checked": 0, "hidden": 0, "missing": [],
             "unreadable": {}}
    heights = mount_heights(constraints)
    pending = dict(heights)   # refs whose height is still to be judged
    cache: dict = {}
    board = _Board(pcb)
    viols = []
    for ref, pos, angle, back, pads, models in footprints(pcb):
        for path, offset, scale, rotate, hidden in models:
            facts["models"] += 1
            if hidden:
                facts["hidden"] += 1
                continue
            f = resolve(path)
            if not f.is_file():
                if path not in facts["missing"]:
                    facts["missing"].append(path)
                continue
            key = (str(f), offset, scale, rotate, back,
                   tuple((n, t, p.wkt) for n, t, p in pads))
            if key not in cache:
                try:
                    mesh = modelgeom.load(f)
                except (OSError, ValueError) as exc:
                    facts["unreadable"][path] = f"{type(exc).__name__}: {exc}"
                    cache[key] = None
                    continue
                cache[key] = (mesh, fit_model(mesh, offset, scale, rotate,
                                              back, pads)) \
                    if mesh is not None else (None, [])
            mesh, found = cache[key] or (None, None)
            if found is None:
                continue
            entry = pending.get(ref)
            if entry is not None and mesh is not None and (
                    entry["model"] in path if entry["model"] else True):
                del pending[ref]
                h, why = judge_mount(entry, mesh, offset, scale, rotate, back,
                                     lambda: board.thickness)
                if why:
                    facts.setdefault("mount_heights_not_judged", {})[ref] = why
                elif h:
                    found = found + [h]
                else:
                    facts.setdefault("mount_heights_ok", []).append(ref)
            if mesh is not None and not any(
                    h["kind"] in ("model_offset", "model_rotation")
                    for h in found) and modelgeom.placed_box(
                    mesh, offset, scale, rotate, back)[2] < -SINK_MM:
                try:
                    region = board.region(pos, angle)
                except Exception as exc:  # noqa: BLE001 - no outline, no test
                    facts["no_outline"] = f"{type(exc).__name__}: {exc}"
                else:
                    h = in_board(mesh, offset, scale, rotate, back, pads,
                                 region, board.thickness)
                    found = found + ([h] if h else [])
            facts["checked"] += 1
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            for h in found:
                fit = " ".join(f"({k} (xyz {' '.join(f'{v:g}' for v in val)}))"
                               for k, val in h["fit"].items()) \
                    if h.get("fit") else ""
                fix = (f"set the footprint's (model ...) to {fit} in its "
                       "library and update the board from it") if fit else \
                    "check the model against the part's drawing"
                extra = {k: v for k, v in h.items()
                         if k not in ("kind", "severity", "msg")}
                viols.append(checklib.violation(
                    SCRIPT, h["severity"], pos, "B.Cu" if back else "F.Cu",
                    None, [ref], f"{ref}: 3D model {name}: {h['msg']}. "
                    f"Fix: {fix}", SOURCE, kind=h["kind"], model_path=path,
                    fix=fix, **extra))
    for ref in pending:
        facts.setdefault("mount_heights_not_judged", {})[ref] = \
            "no shown, readable 3D model for this part on the board"
    return viols, facts


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", help="constraints.json "
                    "(mechanical.mount_heights)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else None
    viols, facts = check(pcb, cons)
    return checklib.report(SCRIPT, pcb, viols, **facts,
                           nothing_to_judge=None if facts.get("checked") else
                           "no 3D model could be read"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
