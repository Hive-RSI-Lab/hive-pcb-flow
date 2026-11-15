#!/usr/bin/env python
"""testprep.py - place the missing test points: for each net with no probe
access, untent a via of the net or add a TP pad on a stub, and write the
ICT probe list (P7 after routing, like route_tune; hive-hwde #549).

Allegro's Testprep, Xpedition's Test Point Insertion and Valor NPI's DFT
advisor do not stop at a coverage report: per net without probe access they
add one on the probe side. This does the same, judged by check_testpoints'
own rules (its access points and its classify), so what it plans is what
the check then counts.

Per net in scope with no access of the asked class (--mode fixture: a
fixture-ok point; flying: a flying-ok point) on the probe side(s):
  1. untent_via  a via of the net on that side, now tented, drill <= 0.5 mm
                 (max_via_drill_mm), whose spot passes the class's rules;
  2. tp_stub     a round TP pad (TestPoint_Pad_D1.0mm) on a short stub
                 (<= --stub-max) from the net's copper on that side, in free
                 space: the pad and stub keep the board clearance from every
                 other net's copper, holes and keepouts, the pad touches no
                 part body, and check_testpoints' rules pass (size, pitch,
                 body clearance by height, edge). Sites outside other nets'
                 pours are tried first; a site in a pour is marked (`in_pour`:
                 the pour refills around it);
  3. via_tp      the net has no copper on that side: a new via on a stub from
                 its copper on another layer, with the TP pad on top of it;
  4. unplaceable with the reason.
Each placement is an obstacle and a pitch neighbour for the next. Power
nets go first, then the rest by name.

The probe list (--csv, fab/ict_testpoints.csv by convention): one row per
net in scope with access - the best existing point (largest) or the planned
one: net, x, y, side, size_mm, kind, ref, pad, source (existing|planned).

--plan (this version: the only mode) edits nothing: it reports what it
would do. Exit 0 when every net in scope has or gets access, 1 when some
net is unplaceable, 2 on an error. Rerunning on a board that has the points places nothing new.

Not yet: the board edit itself, GenCAD $TESTPINS / IPC-D-356 access flags,
tagging fit THT pins.

CLI: --pcb B [--side bottom|top|both] [--nets GLOB ...] [--mode fixture|
     flying] [--plan] [--constraints c.json] [--csv OUT] [--out OUT.json]
     [--tp-mm 1.0] [--stub-max 5] [--grid 0.25]
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import numpy as np  # noqa: E402
import sexpdata  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402
from shapely.ops import nearest_points, unary_union  # noqa: E402

import check_testpoints as ct  # noqa: E402
import check_wicking  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402
from geom import _kid, _strs, _tok  # noqa: E402

SCRIPT = "testprep"
SIDE_OF = {"bottom": ["B"], "top": ["F"], "both": ["B", "F"]}
STUB_W = 0.25          # stub track width, mm
NEW_VIA = (0.6, 0.3)   # a new via's diameter and drill, mm (raised to floors)
MAX_TRIES = 600        # sites confirmed per net and layer before giving up


def ok(p: dict, mode: str) -> bool:
    return bool(p["fixture_ok"] if mode == "fixture" else p["flying_ok"])


def board_state(pcb: Path, cfg: dict) -> dict:
    """check_testpoints' view of the board (its check() up to classify)."""
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    dflt = check_wicking.board_defaults(root)
    lyr = _kid(root, "layers")
    cu = [str(_tok(n[1])) for n in (lyr[1:] if lyr is not None else [])
          if geom._is_node(n) and len(n) > 1
          and str(_tok(n[1])).endswith(".Cu")]
    cu = sorted(set(cu) | {"F.Cu", "B.Cu"},
                key=lambda n: (0 if n == "F.Cu" else 2 if n == "B.Cu" else 1,
                               int(n[2:-3]) if n.startswith("In") else 0))
    vias = check_wicking.read_vias(root, dflt, cu)
    fps = ct.read_board(root, dflt, pcb)
    bg = geom.BoardGeom(pcb, root)
    outline = bg.outline if bg.outline is not None and not bg.outline.is_empty \
        else None
    pts, _stats = ct.access_points(fps, vias, cfg)
    ct.classify(pts, fps, outline, cfg)
    counts = ct.board_nets(fps)
    nets = sorted(n for n, k in counts.items()
                  if k >= 2 and not n.startswith("unconnected-"))
    if cfg["nets"]:
        nets = sorted(n for n in counts
                      if any(fnmatch.fnmatchcase(n, g) for g in cfg["nets"]))
    return {"root": root, "dflt": dflt, "vias": vias, "fps": fps, "bg": bg,
            "outline": outline, "pts": pts, "nets": nets}


def _floors(pcb: Path) -> dict:
    import route_critical  # noqa: PLC0415 - lazy: pulls KRT helpers
    f = route_critical.grading_floors(pcb.with_suffix(".kicad_pro"))
    f.setdefault("hole_to_hole", 0.25)
    return f


class Space:
    """Free space on each copper layer for new copper of one net: other
    nets' copper, holes, keepouts and what this run placed already
    (route_tune.Obstacles, one per layer and pour mode)."""

    def __init__(self, bg, floors: dict, margin: float = 0.0):
        import route_tune  # noqa: PLC0415
        self.bg = bg
        self.clear = floors["clearance"] + margin
        self.h2h = floors["hole_to_hole"]
        self.obs = {p: route_tune.Obstacles(bg, floors["clearance"],
                                            floors["edge_clearance"], margin,
                                            through_pours=p)
                    for p in (False, True)}
        holes = [Point(v.at).buffer(v.drill / 2.0) for v in bg.vias_of()]
        holes += [p.drill_poly for p in bg._pads if p.drill]
        holes += [p.drill_poly for p in bg.npth]
        self.holes = [h for h in holes if not h.is_empty]
        self.npth = [p.drill_poly.buffer(0) for p in bg.npth]
        # every pad, own net's included: no via in a pad, no TP on one
        self.pads = {L: [p.poly for p in bg.pads_of(layer=L)]
                     for L in bg.copper_layers}
        self.pad_tree = {L: shapely.STRtree(v) if v else None
                         for L, v in self.pads.items()}

    def clears(self, poly, layer: str, net: str, pours: bool, own) -> bool:
        if not self.obs[pours].clears(poly, layer, net, own):
            return False
        reach = poly.buffer(self.clear)
        return not any(reach.intersects(h) for h in self.npth)

    def free(self, region, r: float, layer: str, net: str, pours: bool):
        """`region` less where a pad disk of radius r centred would come
        within the clearance of other nets' copper on `layer` (one
        geometric difference; clears() still has the last word)."""
        items, tree = self.obs[pours]._layer_items(layer)
        if tree is None or region.is_empty:
            return region
        grow = r + self.clear - 1e-6
        near = region.buffer(grow)
        bad = []
        for j in tree.query(near, predicate="intersects"):
            onet, opoly, _t = items[int(j)]
            if onet == net:
                continue
            bad.append(opoly.intersection(near).buffer(grow, quad_segs=4))
        t = self.pad_tree.get(layer)
        if t is not None:      # pads of every net: see off_pads
            bad += [self.pads[layer][int(j)].buffer(grow, quad_segs=4)
                    for j in t.query(near, predicate="intersects")]
        for onet, opoly in self.obs[pours].extra.get(layer, []):
            if onet != net and opoly.intersects(near):
                bad.append(opoly.buffer(grow, quad_segs=4))
        return region.difference(unary_union(bad)) if bad else region

    def off_pads(self, poly, layers) -> bool:
        """`poly` keeps the clearance from every pad on `layers` (own net's
        too: a via in a pad wicks solder, a TP on one is no new access)."""
        for L in layers:
            t = self.pad_tree.get(L)
            if t is not None and len(t.query(poly, predicate="dwithin",
                                             distance=self.clear)):
                return False
        return True

    def hole_clear(self, hole) -> bool:
        reach = hole.buffer(self.h2h)
        return not any(reach.intersects(h) for h in self.holes)

    def add(self, layer: str, net: str, poly) -> None:
        for o in self.obs.values():
            o.add(layer, net, poly)

    def add_hole(self, hole) -> None:
        self.holes.append(hole)

    def in_pour(self, poly, layer: str, net: str) -> list[str]:
        return sorted({z.net for z in self.bg.zones_of(layer=layer)
                       if z.net and z.net != net
                       and z.fill_on(layer).intersects(poly)})


def _bodies(fps, s):
    return [f for f in fps if f["body"] is not None and f["side"] == s]


def _confirm(cand: dict, st: dict, placed: list[dict], cfg: dict,
             mode: str) -> bool:
    """check_testpoints' classify on the candidate with its neighbours."""
    spot = Point(cand["spot"])
    near = [dict(p) for p in st["pts"] + placed
            if p["side"] == cand["side"] and p is not cand
            and spot.distance(Point(p["spot"])) < cfg["pitch_mm"] * 3]
    c = dict(cand)
    ct.classify([c] + near, st["fps"], st["outline"], cfg)
    cand.update({k: c[k] for k in ("flying_ok", "fixture_ok",
                                   "fixture_fails")})
    for k in ("pitch_mm", "body_mm", "body_ref", "edge_mm"):
        if c.get(k) is not None:
            cand[k] = c[k]
    return ok(c, mode)


def _exposed(disk, bodies):
    hits = [f["ref"] for f in bodies if f["body"].intersects(disk)]
    if not hits:
        return disk, []
    return disk.difference(unary_union([f["body"] for f in bodies
                                        if f["ref"] in hits])), hits


def untent_candidates(net: str, s: str, st: dict, cfg: dict) -> list[dict]:
    bodies = _bodies(st["fps"], s)
    out = []
    for v in st["vias"]:
        if v["net"] != net or s not in v["sides"] or not v["tent"][s]:
            continue
        if v["drill"] > cfg["max_via_drill_mm"] + ct.EPS:
            continue
        disk = Point(v["at"]).buffer(v["diameter"] / 2.0, quad_segs=32)
        exp, under = _exposed(disk, bodies)
        size, spot = (v["diameter"], v["at"]) if not under else \
            ct._largest_circle(exp)
        if size < ct.EPS:
            continue
        out.append({"kind": "via", "ref": "VIA", "pad": "", "net": net,
                    "side": s, "at": v["at"], "size": size, "spot": spot,
                    "under": under, "blocked": None, "body_side": None,
                    "drill": v["drill"], "uuid": v.get("uuid")})
    out.sort(key=lambda p: -p["size"])
    return out


def _grid(region, step: float) -> np.ndarray:
    if region.is_empty:
        return np.empty((0, 2))
    x0, y0, x1, y1 = region.bounds
    xs = np.arange(np.floor(x0 / step) * step, x1 + step / 2, step)
    ys = np.arange(np.floor(y0 / step) * step, y1 + step / 2, step)
    gx, gy = np.meshgrid(xs, ys)
    xy = np.column_stack([gx.ravel(), gy.ravel()])
    keep = shapely.contains_xy(region, xy[:, 0], xy[:, 1])
    return xy[keep]


def _anchor(bg, net: str, layer: str):
    memo = bg.__dict__.setdefault("_testprep_anchor", {})
    if (net, layer) not in memo:
        memo[(net, layer)] = _anchor_of(bg, net, layer)
    return memo[(net, layer)]


def _anchor_of(bg, net: str, layer: str):
    parts = [t.poly for t in bg.tracks_of(net, layer)]
    parts += [p.poly for p in bg.pads_of(net, layer)]
    parts += [v.poly for v in bg.vias_of(net, layer)]
    parts += [z.fill_on(layer) for z in bg.zones_of(net, layer)]
    parts = [p for p in parts if not p.is_empty]
    return unary_union(parts) if parts else None


def site_search(net: str, s: str, anchor_layer: str, st: dict, space: Space,
                placed: list[dict], cfg: dict, mode: str, opts: dict,
                pours: bool) -> dict | None:
    """The nearest site (shortest stub) for a TP pad on side `s` fed from
    the net's copper on `anchor_layer` (a new via joins them when they
    differ) that clears the space and passes check_testpoints' rules."""
    bg = st["bg"]
    layer = f"{s}.Cu"
    anchor = _anchor(bg, net, anchor_layer)
    if anchor is None:
        return None
    r = opts["tp_mm"] / 2.0
    via = anchor_layer != layer
    vd, vh = opts["via"]
    edge_in = max(cfg["edge_mm"] if mode == "fixture" else 0.0,
                  opts["edge_clearance"] + r)
    region = anchor.buffer(opts["stub_max"] + r)
    if st["outline"] is not None:
        region = region.intersection(st["outline"].buffer(-edge_in))
    region = space.free(region, r, layer, net, pours)
    xy = _grid(region, opts["grid"])
    if not len(xy):
        return None
    bodies = _bodies(st["fps"], s)
    if bodies:
        btree = shapely.STRtree([f["body"] for f in bodies])
        far = np.ones(len(xy), bool)
        hit = btree.query(shapely.points(xy), predicate="dwithin",
                          distance=r + 0.05)
        far[hit[0]] = False
        xy = xy[far]
    d = shapely.distance(anchor, shapely.points(xy))
    order = np.argsort(d, kind="stable")
    own = {layer: set(bg.tracks_of(net, layer)),
           anchor_layer: set(bg.tracks_of(net, anchor_layer))}
    tries = 0
    for i in order:
        site = Point(xy[i])
        disk = site.buffer(r, quad_segs=32)
        if not space.clears(disk, layer, net, pours, own[layer]):
            continue
        if not space.off_pads(disk, [layer]):
            continue
        stub = None
        if d[i] > (vd / 2.0 if via else r) - 0.01:
            a, _ = nearest_points(anchor, site)
            stub = LineString([(a.x, a.y), (site.x, site.y)])
            if not space.clears(stub.buffer(opts["stub_w"] / 2.0),
                                anchor_layer, net, pours,
                                own[anchor_layer]):
                continue
        hole = None
        if via:
            vdisk = site.buffer(vd / 2.0, quad_segs=32)
            hole = site.buffer(vh / 2.0, quad_segs=16)
            if not space.hole_clear(hole):
                continue
            if not space.off_pads(vdisk, bg.copper_layers):
                continue
            if not all(space.clears(vdisk, L, net, L != anchor_layer
                                    or pours, own.get(L, set()))
                       for L in bg.copper_layers if L != layer):
                continue
        tries += 1
        if tries > MAX_TRIES:
            return None
        cand = {"kind": "tp", "ref": "TP?", "pad": "1", "net": net,
                "side": s, "at": (site.x, site.y), "size": opts["tp_mm"],
                "spot": (site.x, site.y), "under": [], "blocked": None,
                "body_side": s, "drill": None}
        if not _confirm(cand, st, placed, cfg, mode):
            continue
        cand["stub"] = None if stub is None else \
            [[checklib.rnd(c, 3) for c in pt] for pt in stub.coords]
        cand["stub_mm"] = 0.0 if stub is None else checklib.rnd(stub.length, 3)
        cand["anchor_layer"] = anchor_layer
        cand["geom"] = {"disk": disk, "stub": stub, "hole": hole,
                        "vdisk": site.buffer(vd / 2.0) if via else None}
        cand["in_pour"] = space.in_pour(
            disk if stub is None else disk.union(stub.buffer(
                opts["stub_w"] / 2.0)), layer, net)
        return cand
    return None


def _anchor_layers(bg, net: str, s: str) -> list[str]:
    """Layers with the net's copper to stub from, the far outer one first."""
    far = "F.Cu" if s == "B" else "B.Cu"
    have = [L for L in bg.copper_layers if _anchor(bg, net, L) is not None
            and L != f"{s}.Cu"]
    return sorted(have, key=lambda L: (L != far, bg.copper_layers.index(L)))


def plan(pcb: Path, side: str = "bottom", nets: list[str] | None = None,
         mode: str = "fixture", constraints: dict | None = None,
         tp_mm: float = 1.0, stub_max: float = 5.0, grid: float = 0.25,
         margin: float = 0.0) -> dict:
    cfg = ct._cfg(constraints)
    if nets:
        cfg["nets"] = nets
    st = board_state(pcb, cfg)
    bg, sides = st["bg"], SIDE_OF[side]
    floors = _floors(pcb)
    vd = max(NEW_VIA[0], floors.get("via_diameter", 0.0))
    vh = max(NEW_VIA[1], floors.get("via_drill", 0.0))
    opts = {"tp_mm": tp_mm, "stub_max": stub_max, "grid": grid,
            "stub_w": max(STUB_W, floors.get("track_width", 0.0)),
            "edge_clearance": floors["edge_clearance"], "via": (vd, vh)}
    space = Space(bg, floors, margin)
    access = {n: [p for p in st["pts"] if p["net"] == n and p["side"] in sides
                  and ok(p, mode)] for n in st["nets"]}
    missing = [n for n in st["nets"] if not access[n]]
    missing.sort(key=lambda n: (not ct.is_power(n), n))
    placed, actions = [], []
    for net in missing:
        act = None
        why = []
        for s in sides:
            for cand in untent_candidates(net, s, st, cfg):
                if _confirm(cand, st, placed, cfg, mode):
                    act = {"action": "untent_via", "point": cand}
                    break
                why.append(f"via at {_xy(cand['at'])} on the "
                           f"{ct.SIDE_NAME[s]}: "
                           f"{', '.join(cand['fixture_fails']) or 'small'}")
            if act:
                break
        if act is None:
            for pours in (False, True):
                for s in sides:
                    layer = f"{s}.Cu"
                    if _anchor(bg, net, layer) is not None:
                        c = site_search(net, s, layer, st, space, placed, cfg,
                                        mode, opts, pours)
                        if c:
                            act = {"action": "tp_stub", "point": c}
                            break
                    for L in _anchor_layers(bg, net, s):
                        c = site_search(net, s, L, st, space, placed, cfg,
                                        mode, opts, pours)
                        if c:
                            act = {"action": "via_tp", "point": c}
                            break
                    if act:
                        break
                if act:
                    break
        if act is None:
            has_cu = any(_anchor(bg, net, L) is not None
                         for L in bg.copper_layers)
            why.append("no free site within "
                       f"{stub_max:g} mm of its copper that clears the "
                       "board's clearance and the test-point rules"
                       if has_cu else "the net has no copper on the board")
            actions.append({"net": net, "action": "unplaceable",
                            "why": why})
            continue
        p = act["point"]
        placed.append(p)
        g = p.pop("geom", None)
        if g:
            space.add(f"{p['side']}.Cu", net, g["disk"])
            if g["stub"] is not None:
                space.add(p["anchor_layer"], net,
                          g["stub"].buffer(opts["stub_w"] / 2.0))
            if g["vdisk"] is not None:
                for L in bg.copper_layers:
                    space.add(L, net, g["vdisk"])
                space.add_hole(g["hole"])
        actions.append({"net": net, "action": act["action"],
                        **_act_fact(p, act["action"], opts)})
    rows = probe_rows(st, access, placed, mode)
    n = len(st["nets"])
    before = sum(1 for k in st["nets"] if access[k])
    counts = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    return {
        "script": SCRIPT,
        "status": "violations" if counts.get("unplaceable") else "pass",
        "board": str(pcb), "mode": mode, "side": side,
        "plan_only": True, "nets": n,
        "nets_scope": "constraints/--nets" if cfg["nets"]
        else "all nets with 2+ pads",
        "covered_before": before,
        "coverage_before_pct": checklib.rnd(100.0 * before / n, 1) if n
        else 100.0,
        "covered_after": before + len(placed),
        "coverage_after_pct": checklib.rnd(100.0 * (before + len(placed)) / n,
                                           1) if n else 100.0,
        "actions_by_kind": counts, "actions": actions,
        "probe_list": rows, "floors": floors,
        "config": {**{k: cfg[k] for k in ct.DEFAULTS},
                   "tp_mm": tp_mm, "stub_max": stub_max,
                   "stub_w": opts["stub_w"], "new_via": [vd, vh]}}


def _xy(at) -> str:
    return f"({at[0]:.2f}, {at[1]:.2f})"


def _act_fact(p: dict, action: str, opts: dict) -> dict:
    out = {"side": ct.SIDE_NAME[p["side"]],
           "at": [checklib.rnd(c, 3) for c in p["spot"]],
           "size_mm": checklib.rnd(p["size"], 3)}
    if action == "untent_via":
        out["via_at"] = [checklib.rnd(c, 3) for c in p["at"]]
        out["drill_mm"] = p["drill"]
        if p.get("uuid"):
            out["via_uuid"] = p["uuid"]
    else:
        out["footprint"] = (f"TestPoint:TestPoint_Pad_D{opts['tp_mm']:g}mm")
        out["stub"] = p.get("stub")
        out["stub_mm"] = p.get("stub_mm")
        out["stub_layer"] = p.get("anchor_layer")
        if action == "via_tp":
            out["new_via"] = list(opts["via"])
        if p.get("in_pour"):
            out["in_pour"] = p["in_pour"]
    for k in ("pitch_mm", "body_mm", "body_ref", "edge_mm"):
        if p.get(k) is not None:
            out[k] = p[k]
    return out


def probe_rows(st: dict, access: dict, placed: list[dict], mode: str
               ) -> list[dict]:
    rows = []
    new = {p["net"]: p for p in placed}
    for n in st["nets"]:
        if access[n]:
            p, src = max(access[n], key=lambda q: (q["size"], q["kind"] == "tp")), \
                "existing"
        elif n in new:
            p, src = new[n], "planned"
        else:
            continue
        rows.append({"net": n, "x": checklib.rnd(p["spot"][0], 3),
                     "y": checklib.rnd(p["spot"][1], 3),
                     "side": ct.SIDE_NAME[p["side"]],
                     "size_mm": checklib.rnd(p["size"], 3),
                     "kind": "via" if p["kind"] == "via" else
                     "tp" if p["kind"] == "tp" else "pad",
                     "ref": p["ref"], "pad": p["pad"], "source": src})
    return rows


CSV_COLS = ("net", "x", "y", "side", "size_mm", "kind", "ref", "pad", "source")


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in CSV_COLS})


def summary(rep: dict) -> str:
    out = [f"{rep['board']}: {rep['nets']} nets, {rep['mode']} access on the "
           f"{rep['side']}: {rep['covered_before']} "
           f"({rep['coverage_before_pct']} %) -> planned "
           f"{rep['covered_after']} ({rep['coverage_after_pct']} %)",
           "  " + ", ".join(f"{k} {v}" for k, v in
                            sorted(rep["actions_by_kind"].items()))]
    for a in rep["actions"]:
        if a["action"] == "unplaceable":
            out.append(f"  {a['net']:24} unplaceable: {'; '.join(a['why'])}")
        else:
            extra = ""
            if a.get("stub_mm"):
                extra = f" stub {a['stub_mm']} mm on {a['stub_layer']}"
            if a.get("in_pour"):
                extra += f" (in the {', '.join(a['in_pour'])} pour)"
            out.append(f"  {a['net']:24} {a['action']:11} {a['side']} at "
                       f"{_xy(a['at'])} {a['size_mm']} mm{extra}")
    return "\n".join(out)


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--side", choices=sorted(SIDE_OF), default="bottom")
    ap.add_argument("--nets", nargs="+", help="net globs (default: all nets "
                    "with 2+ pads, or constraints testpoints.nets)")
    ap.add_argument("--mode", choices=("fixture", "flying"), default="fixture")
    ap.add_argument("--plan", action="store_true",
                    help="report what would be placed; edit nothing")
    ap.add_argument("--constraints")
    ap.add_argument("--csv", help="write the ICT probe list here")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--tp-mm", type=float, default=1.0)
    ap.add_argument("--stub-max", type=float, default=5.0)
    ap.add_argument("--grid", type=float, default=0.25)
    ap.add_argument("--margin", type=float, default=0.0,
                    help="added to the board clearance, mm")
    a = ap.parse_args(argv)
    if not a.plan:
        raise checklib.CheckError(
            "testprep edits no board yet: run it with --plan (the board "
            "edit is #549's next step)")
    pcb = Path(a.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = checklib.load_json(a.constraints, "constraints") \
        if a.constraints and Path(a.constraints).is_file() else None
    rep = plan(pcb, a.side, a.nets, a.mode, cons, a.tp_mm, a.stub_max,
               a.grid, a.margin)
    if a.csv:
        write_csv(rep["probe_list"], Path(a.csv))
    print(summary(rep), file=sys.stderr)
    return rep, a.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
