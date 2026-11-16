#!/usr/bin/env python
"""check_testpoints.py - test-point access and coverage for flying-probe and
bed-of-nails (ICT) test, and the Tag-Connect keep-out (P8 verify; board only;
hive-hwde #470).

An in-circuit or flying-probe tester can only judge a net its probe can
touch. Valor NPI's DFT analysis and the ICT fixture-prep / flying-probe CAM
tools (Keysight, Teradyne, Seica, Takaya) list, for every net, its probe-able
access points and report coverage; a net with none is untestable. This
check does the same from the board file.

Access points, per side (top = F, bottom = B):
  TP footprints   every copper pad of a test-point footprint (ref TP<n> or a
                  TestPoint library footprint) open in that side's mask
  pads            any other part's SMD or plated-through pad open in the
                  mask, where it reaches out from under the part bodies on
                  that side (a THT pad on the far side is clear of its own
                  body)
  vias            a via untented on that side with a drill <= 0.5 mm (a
                  bigger hole swallows the probe tip), clear of bodies

Each point's size is the diameter of the largest circle that fits on its
exposed copper (shapely's maximum inscribed circle of copper AND mask
opening, less part bodies), and its probe spot is that circle's centre.

  flying ok    size >= 0.5 mm (probe_mm)
  fixture ok   flying ok, and size >= 0.8 mm (fixture_mm), >= 1.27 mm
               (pitch_mm) from the next fixture-sized point of another net
               on the same side, >= max(1 mm, 0.3 x height) (body_mm,
               body_h_ratio) from every part body on that side (its own
               included, except a test point's), and its spot >= 3 mm
               (edge_mm) from the board edge

Sources: FixturFab and Genesys DFT guidelines (0.9-1.0 mm pad, 0.8 min;
2.54 mm pitch preferred, 1.27 min; >= 1 mm to parts, more for tall ones;
~3 mm from the edge for the fixture's gasket/tooling); flying-probe
vendors take 0.5 mm pads; reference/ingest/dfm-assembly.md gap 4.

Part bodies are the footprint's F.Fab/B.Fab outline (its convex hull), else
the footprint of its 3D model's bounding box (VRML, matinglib), else
unknown: its pads then count as open and the part is listed in
`body_unknown` (a courtyard is no body: it always covers the pads). Heights come from the footprint/model name (-H<mm>) or its
VRML model (matinglib.part_height); unknown heights use the 1 mm floor
(`height_unknown` counts them). Test points, fiducials, mounting holes, net
ties, logos and Tag-Connect pads have no body.

Coverage is taken over the board's nets with two or more pads (a one-pad
net has nothing to test between), or the constraints `testpoints.nets`
globs: one-side coverage for top and bottom (fixture and flying), and
either-side flying coverage. `nets_no_access` lists the nets with no
flying-ok point on either side.

Tag-Connect (TC2030/TC2050/TC2070, -NL or -FP): the plug's pogo pins land
on the contact pads, so the keep-out between them (the footprint's own
keep-out rule area when it has one, else the rectangle through the
contact-pad centres - KiCad's stock rule area) must hold no via and no
track on the contact side, and the contacts must carry no paste.

Findings (warnings):
  tp_unusable       a test-point footprint's pad no probe can use (tented,
                    under a body, smaller than probe_mm)
  tp_rail_no_access a power net (GND, V*, +*, *V3...) with no flying-ok
                    access point: bring-up probes it by hand
  tp_coverage_low   either-side flying coverage under constraints
                    testpoints.min_coverage_pct (only when set)
  tp_fixture        a test point that misses a fixture rule, only when
                    constraints testpoints.fixture is true
  tc_keepout_via    a via inside a Tag-Connect keep-out
  tc_keepout_track  a track on the contact side crossing it
  tc_paste          paste on a Tag-Connect contact pad

Constraints (optional) `testpoints`: {nets: [globs], min_coverage_pct,
fixture: bool, probe_mm, fixture_mm, pitch_mm, body_mm, body_h_ratio,
edge_mm, max_via_drill_mm}.

Cross-check (tests/test_check_testpoints.py, slow): each pad's and via's
copper side and position against kicad-cli's IPC-D-356 records (lib/
ipcd356.py, the netlist fab_export ships) on pinned lite-main.

CLI: --pcb board.kicad_pcb [--constraints c.json] [--all-points]
     [--out report.json]; exit 0 pass / 1 violations / 2 error.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import sexpdata
import shapely
from shapely import affinity
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import check_paste  # noqa: E402
import check_wicking  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402
from geom import _kid, _kids, _nums, _strs, _tok, fp_field  # noqa: E402

SCRIPT = "check_testpoints"
SOURCE = SCRIPT
SIDES = ("F", "B")
SIDE_NAME = {"F": "top", "B": "bottom"}

DEFAULTS = {
    "probe_mm": 0.5,        # flying probe: smallest pad it lands on
    "fixture_mm": 0.8,      # bed of nails: smallest target
    "pitch_mm": 1.27,       # bed of nails: closest probe pitch (50 mil)
    "body_mm": 1.0,         # target to part body
    "body_h_ratio": 0.3,    # ... or 0.3 x the part's height when larger
    "edge_mm": 3.0,         # probe spot to board edge
    "max_via_drill_mm": 0.5,
}

TP_REF = re.compile(r"^TP\d", re.I)
TP_FPID = re.compile(r"(^|[:_])TestPoint", re.I)
TC_FPID = re.compile(r"Tag-?Connect|TC20[357]0", re.I)
NO_BODY_FPID = re.compile(
    r"Fiducial|MountingHole|NetTie|Net_Tie|Logo|Symbol:|TestPoint|"
    r"Tag-?Connect|TC20[357]0|Jumper.*(Bridged|Open)|SolderJumper", re.I)
POWER_NET = re.compile(
    r"^/?(?:.*/)?(?:[AD]?GND\w*|PGND|GNDA|GNDD|VSS\w*|VCC\w*|VDD\w*|VBUS\w*|"
    r"VBAT\w*|VIN\w*|VSYS\w*|VREF\w*|VMOT\w*|\+.*|-\d.*|"
    r"\d+V\d*\w*|V\d+\w*|\w*_\d+V\d*|\w*\d+V\d+\w*)$", re.I)
EPS = 1e-6


def is_power(net: str) -> bool:
    return bool(net) and POWER_NET.match(net) is not None


# ------------------------------------------------------------ board read

def _ref(fp) -> str:
    return fp_field(fp, "Reference", "?")


def _graphic_points(fp, layer: str) -> list[tuple[float, float]]:
    """Footprint-local points of every graphic on `layer` (lines, rects,
    circles, arcs, polys)."""
    pts = []
    for g in fp[1:]:
        if not geom._is_node(g) or str(_tok(g[0])) not in (
                "fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            continue
        lay = _kid(g, "layer")
        if lay is None or not _strs(lay) or _strs(lay)[0] != layer:
            continue
        kind = str(_tok(g[0]))
        if kind == "fp_poly":
            pts.extend(geom._pts(_kid(g, "pts") or g))
            continue
        s, e = _kid(g, "start"), _kid(g, "end")
        if kind == "fp_circle" and s is not None and e is not None:
            c, q = _nums(s)[:2], _nums(e)[:2]
            r = ((q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2) ** 0.5
            pts.extend((c[0] + dx * r, c[1] + dy * r)
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            continue
        for n in (s, _kid(g, "mid"), e):
            if n is not None:
                pts.append(tuple(_nums(n)[:2]))
        if kind == "fp_rect" and s is not None and e is not None:
            a, b = _nums(s)[:2], _nums(e)[:2]
            pts += [(a[0], b[1]), (b[0], a[1])]
    return pts


def _hull(pts) -> Polygon | None:
    if len(pts) < 3:
        return None
    h = MultiPoint(pts).convex_hull
    return h if h.geom_type == "Polygon" and h.area > 1e-4 else None


def _place(local, fx, fy, fa):
    g = affinity.rotate(local, -fa, origin=(0, 0), use_radians=False)
    return affinity.translate(g, fx, fy)


def _keepout_areas(fp, fx, fy, fa) -> list[dict]:
    """A footprint's own keep-out rule areas, in the board frame."""
    out = []
    for z in _kids(fp, "zone"):
        ko = _kid(z, "keepout")
        poly = _kid(z, "polygon")
        if ko is None or poly is None:
            continue
        flags = {str(_tok(k[0])): str(_tok(k[1])) for k in ko[1:]
                 if geom._is_node(k) and len(k) > 1}
        pts = geom._pts(_kid(poly, "pts") or poly)
        if len(pts) < 3:
            continue
        lay = _kid(z, "layer")
        lays = _kid(z, "layers")
        layers = (_strs(lay) if lay is not None else
                  _strs(lays) if lays is not None else [])
        out.append({"poly": _place(Polygon(pts), fx, fy, fa),
                    "vias": flags.get("vias") != "allowed",
                    "tracks": flags.get("tracks") != "allowed",
                    "layers": layers})
    return out


def read_board(root, dflt: dict, pcb: Path) -> list[dict]:
    """Every footprint: ref, fpid, side, flags, pads (net, copper, mask
    openings per side, copper sides, paste), body polygon, height."""
    import matinglib
    metas = matinglib.board_meta(pcb)
    fps = []
    legacy = geom.legacy_fill(root)
    for fp in _kids(root, "footprint"):
        fpid = str(_tok(fp[1])) if len(fp) > 1 and not geom._is_node(fp[1]) \
            else ""
        at = _kid(fp, "at")
        fn = _nums(at) if at is not None else [0, 0, 0]
        fx, fy, fa = fn[0], fn[1], (fn[2] if len(fn) > 2 else 0.0)
        lay = _kid(fp, "layer")
        side = "B" if lay is not None and _strs(lay) and \
            _strs(lay)[0] == "B.Cu" else "F"
        ref = _ref(fp)
        attr = _kid(fp, "attr")
        attrs = {str(_tok(a)) for a in (attr[1:] if attr is not None else [])
                 if not geom._is_node(a)}
        is_tp = bool(TP_REF.match(ref)) or bool(TP_FPID.search(fpid))
        is_tc = bool(TC_FPID.search(fpid))
        f_mm = _kid(fp, "solder_mask_margin")
        f_mm = _nums(f_mm)[0] if f_mm is not None and _nums(f_mm) else None
        pads = []
        for pad in _kids(fp, "pad"):
            if len(pad) < 4:
                continue
            kind, shape = str(_tok(pad[2])), str(_tok(pad[3]))
            if kind not in ("smd", "thru_hole", "connect"):
                continue
            pat, size = _kid(pad, "at"), _kid(pad, "size")
            if pat is None or size is None:
                continue
            layers = check_paste._layers(pad)
            lx, ly, *rest = _nums(pat)
            angle = rest[0] if rest else 0.0
            dx, dy = geom._rot(lx, ly, -fa)
            c = (fx + dx, fy + dy)
            sz = _nums(size)
            w, h = sz[0], (sz[1] if len(sz) > 1 else sz[0])
            rr = _kid(pad, "roundrect_rratio")
            rratio = _nums(rr)[0] if rr is not None and _nums(rr) else 0.0
            if shape == "custom" and \
                    (cg := geom._custom_pad(pad, w, h, legacy)) is not None:
                cg = affinity.rotate(cg, -angle, origin=(0, 0),
                                     use_radians=False)
                copper = affinity.translate(cg, c[0], c[1])
            else:
                copper = geom._pad_polygon(
                    "rect" if shape == "custom" else shape, w, h, rratio, c,
                    angle)
            cu = {s for s in SIDES
                  if f"{s}.Cu" in layers or "*.Cu" in layers}
            if kind != "thru_hole" and len(cu) > 1:
                cu = {side}
            mmn = _kid(pad, "solder_mask_margin")
            mmn = _nums(mmn)[0] if mmn is not None and _nums(mmn) else None
            mm = next(v for v in (mmn, f_mm, dflt["mask_margin"])
                      if v is not None)
            mask = {s: (copper.buffer(mm, join_style="mitre") if mm
                        else copper)
                    for s in cu
                    if f"{s}.Mask" in layers or "*.Mask" in layers}
            net = _kid(pad, "net")
            ns = _strs(net) if net is not None else []
            drill = _kid(pad, "drill")
            dn = _nums(drill) if drill is not None else []
            pads.append({
                "number": str(_tok(pad[1])), "kind": kind,
                "net": ns[-1] if ns else "", "copper": copper,
                "center": c, "cu": cu, "mask": mask,
                "paste": {s for s in SIDES
                          if f"{s}.Paste" in layers or "*.Paste" in layers},
                "drill": dn[0] if dn else None})
        body, body_src = None, None
        has_body = not (is_tp or is_tc or NO_BODY_FPID.search(fpid)
                        or "board_only" in attrs or ref.startswith("#"))
        m = metas.get(ref) or {}
        if has_body:
            lb = _hull(_graphic_points(fp, f"{side}.Fab"))
            body_src = "fab"
            if lb is None:
                # the 3D model's footprint (its box, leads included: a
                # chip's terminations are body too, only the toe is free)
                corners = matinglib.model_corners_local(
                    SimpleNamespace(side="back" if side == "B" else "front"),
                    m, pcb) if m.get("model") else None
                lb = _hull(corners or [])
                body_src = "model"
            if lb is not None:
                body = _place(lb, fx, fy, fa)
            else:
                body_src = "unknown"
        height = None
        if body is not None:
            height = matinglib.part_height(
                SimpleNamespace(fpid=fpid, ref=ref), m.get("model"), pcb)
        fps.append({
            "ref": ref, "fpid": fpid, "side": side, "pos": (fx, fy),
            "is_tp": is_tp, "is_tc": is_tc, "pads": pads, "body": body,
            "body_src": body_src,
            "height": height,
            "keepouts": _keepout_areas(fp, fx, fy, fa) if is_tc else []})
    return fps


# ------------------------------------------------------------ access

def _largest_circle(g):
    """(diameter, centre) of the largest circle inside g (its biggest
    piece), or (0, None)."""
    if g is None or g.is_empty:
        return 0.0, None
    parts = list(g.geoms) if hasattr(g, "geoms") else [g]
    parts = [p for p in parts if p.geom_type == "Polygon" and p.area > 1e-6]
    if not parts:
        return 0.0, None
    p = max(parts, key=lambda q: q.area)
    line = shapely.maximum_inscribed_circle(p, tolerance=0.002)
    c = line.coords[0]
    return 2.0 * line.length, (c[0], c[1])


def _round_up(v: float) -> float:
    return round(v, 3)


def access_points(fps: list[dict], vias: list[dict], cfg: dict
                  ) -> tuple[list[dict], dict]:
    """Every probe-able point on either side, unclassified beyond size."""
    bodies = {s: [(f, f["body"]) for f in fps
                  if f["body"] is not None and f["side"] == s] for s in SIDES}
    trees = {s: STRtree([b for _, b in bodies[s]]) if bodies[s] else None
             for s in SIDES}
    stats = {"pads_open": 0, "pads_covered": 0, "vias_tented": 0,
             "vias_big_drill": 0, "vias_under_body": 0}

    def uncovered(g, s):
        if trees[s] is None:
            return g, []
        hits = [bodies[s][i] for i in trees[s].query(g)]
        hits = [(f, b) for f, b in hits if b.intersects(g)]
        if not hits:
            return g, []
        return g.difference(unary_union([b for _, b in hits])), \
            [f["ref"] for f, _ in hits]

    pts = []
    for f in fps:
        if f["is_tc"]:
            continue
        for p in f["pads"]:
            if not p["net"]:
                continue
            for s in sorted(p["cu"]):
                if s not in p["mask"]:
                    if f["is_tp"]:
                        pts.append(_pt(f, p, s, 0.0, p["center"],
                                       "tented", []))
                    continue
                open_cu = p["copper"].intersection(p["mask"][s])
                if p["kind"] == "thru_hole" and p["drill"]:
                    # a lead fills the hole; the probe lands on the ring
                    # (or the lead tip: any spot on the pad is copper)
                    pass
                exposed, under = uncovered(open_cu, s)
                size, spot = _largest_circle(exposed)
                if under and size < EPS:
                    stats["pads_covered"] += 1
                    if f["is_tp"]:
                        pts.append(_pt(f, p, s, 0.0, p["center"],
                                       "under_body", under))
                    continue
                stats["pads_open"] += 1
                pts.append(_pt(f, p, s, size, spot, None, under))
    for v in vias:
        if not v["net"]:
            continue
        for s in sorted(v["sides"]):
            if v["tent"][s]:
                stats["vias_tented"] += 1
                continue
            if v["drill"] > cfg["max_via_drill_mm"] + EPS:
                stats["vias_big_drill"] += 1
                continue
            cu = Point(v["at"]).buffer(v["diameter"] / 2.0, quad_segs=32)
            exposed, under = uncovered(cu, s)
            if under and exposed.area < 1e-6:
                stats["vias_under_body"] += 1
                continue
            if under:
                size, spot = _largest_circle(exposed)
            else:
                size, spot = v["diameter"], v["at"]
            pts.append({"kind": "via", "ref": "VIA", "pad": "",
                        "net": v["net"], "side": s,
                        "at": v["at"], "size": size, "spot": spot,
                        "under": under, "blocked": None, "body_side": None,
                        "drill": v["drill"]})
    return pts, stats


def _pt(f, p, s, size, spot, blocked, under) -> dict:
    return {"kind": "tp" if f["is_tp"] else p["kind"], "ref": f["ref"],
            "pad": p["number"], "net": p["net"], "side": s,
            "at": p["center"], "size": size, "spot": spot or p["center"],
            "under": under, "blocked": blocked, "body_side": f["side"],
            "drill": p["drill"]}


def classify(pts: list[dict], fps: list[dict], outline, cfg: dict) -> dict:
    """Mark each point flying_ok / fixture_ok with the reasons it fails."""
    edge = outline.boundary if outline is not None and \
        not outline.is_empty else None
    by_ref = {f["ref"]: f for f in fps}
    bodies = {s: [f for f in fps if f["body"] is not None and f["side"] == s]
              for s in SIDES}
    trees = {s: STRtree([f["body"] for f in bodies[s]]) if bodies[s] else None
             for s in SIDES}
    unknown_h = set()
    for p in pts:
        why = []
        if p["blocked"]:
            why.append(p["blocked"])
        elif p["size"] < cfg["probe_mm"] - EPS:
            why.append("small")
        p["flying_ok"] = not why
    for s in SIDES:
        cand = [p for p in pts if p["side"] == s and p["flying_ok"]
                and p["size"] >= cfg["fixture_mm"] - EPS]
        tree = STRtree([Point(p["spot"]) for p in cand]) if cand else None
        for p in pts:
            if p["side"] != s:
                continue
            fx = []
            if not p["flying_ok"]:
                fx.append(p["blocked"] or "small")
            elif p["size"] < cfg["fixture_mm"] - EPS:
                fx.append("small")
            spot = Point(p["spot"])
            # pitch to the next fixture-sized point of another net
            pitch = None
            if tree is not None:
                for i in tree.query(spot.buffer(cfg["pitch_mm"] * 3)):
                    q = cand[int(i)]
                    if q is p or q["net"] == p["net"]:
                        continue
                    d = spot.distance(Point(q["spot"]))
                    pitch = d if pitch is None else min(pitch, d)
            p["pitch_mm"] = None if pitch is None else checklib.rnd(pitch, 3)
            if pitch is not None and pitch < cfg["pitch_mm"] - EPS:
                fx.append("pitch")
            # body clearance from the probe's landing circle
            land = spot.buffer(max(p["size"], 1e-3) / 2.0)
            worst = None
            if trees[s] is not None:
                reach = cfg["body_mm"] + 20.0
                for i in trees[s].query(land.buffer(reach)):
                    f = bodies[s][int(i)]
                    if p["kind"] == "tp" and f["ref"] == p["ref"]:
                        continue
                    h = f["height"]
                    if h is None:
                        unknown_h.add(f["ref"])
                    need = max(cfg["body_mm"],
                               cfg["body_h_ratio"] * (h or 0.0))
                    d = land.distance(f["body"])
                    if d < need - EPS and (worst is None
                                           or d - need < worst[0]):
                        worst = (d - need, f["ref"], d, need)
            p["body_mm"] = None
            own = by_ref.get(p["ref"])
            if p["kind"] not in ("tp", "via") and own is not None \
                    and own["body_src"] == "unknown":
                fx.append("body_unknown")
            if worst is not None:
                fx.append("body")
                p["body_mm"] = checklib.rnd(worst[2], 3)
                p["body_ref"] = worst[1]
                p["body_need_mm"] = checklib.rnd(worst[3], 3)
            if edge is not None:
                de = spot.distance(edge)
                p["edge_mm"] = checklib.rnd(de, 3)
                if de < cfg["edge_mm"] - EPS:
                    fx.append("edge")
            p["fixture_ok"] = not fx
            p["fixture_fails"] = fx
    return {"height_unknown": sorted(unknown_h)}


# ------------------------------------------------------------ coverage

def board_nets(fps: list[dict]) -> dict[str, int]:
    n: dict[str, int] = {}
    for f in fps:
        for p in f["pads"]:
            if p["net"]:
                n[p["net"]] = n.get(p["net"], 0) + 1
    return n


def coverage(pts: list[dict], nets: list[str]) -> dict:
    def cov(pred):
        hit = {p["net"] for p in pts if pred(p)}
        k = sum(1 for n in nets if n in hit)
        return checklib.rnd(100.0 * k / len(nets), 1) if nets else 100.0
    return {
        "flying_top": cov(lambda p: p["side"] == "F" and p["flying_ok"]),
        "flying_bottom": cov(lambda p: p["side"] == "B" and p["flying_ok"]),
        "flying_either": cov(lambda p: p["flying_ok"]),
        "fixture_top": cov(lambda p: p["side"] == "F" and p["fixture_ok"]),
        "fixture_bottom": cov(lambda p: p["side"] == "B"
                              and p["fixture_ok"]),
    }


# ------------------------------------------------------------ Tag-Connect

def tag_connect(fps: list[dict], root, vias: list[dict]) -> tuple[list, list]:
    vios, facts = [], []
    tcs = [f for f in fps if f["is_tc"]]
    if not tcs:
        return vios, facts
    tracks = []
    for t in _kids(root, "segment") + _kids(root, "arc"):
        s, e, lay = _kid(t, "start"), _kid(t, "end"), _kid(t, "layer")
        w = _kid(t, "width")
        if s is None or e is None or lay is None:
            continue
        tracks.append({"line": LineString([tuple(_nums(s)[:2]),
                                           tuple(_nums(e)[:2])]),
                       "layer": _strs(lay)[0],
                       "width": _nums(w)[0] if w is not None else 0.0,
                       "net": (_strs(_kid(t, "net"))[-1]
                               if _kid(t, "net") is not None
                               and _strs(_kid(t, "net")) else "")})
    for f in tcs:
        contacts = [p for p in f["pads"] if p["kind"] in ("smd", "connect")]
        if not contacts:
            continue
        side = next(iter(contacts[0]["cu"]), f["side"])
        kos = f["keepouts"]
        if not kos:
            r = MultiPoint([p["center"] for p in contacts]).envelope
            if r.geom_type != "Polygon":
                r = r.buffer(0.3)
            kos = [{"poly": r, "vias": True, "tracks": True,
                    "layers": [f"{side}.Cu"], "default": True}]
        fact = {"ref": f["ref"], "fpid": f["fpid"], "side": SIDE_NAME[side],
                "keepout": "footprint" if f["keepouts"] else "pad_centres",
                "vias": [], "tracks": [], "paste": []}
        area = unary_union([k["poly"] for k in kos])
        # a via anywhere in the keep-out (the stock area forbids them; the
        # issue asks it of every Tag-Connect)
        for v in vias:
            vp = Point(v["at"]).buffer(v["diameter"] / 2.0)
            if vp.intersection(area).area > 1e-4:
                fact["vias"].append([checklib.rnd(c) for c in v["at"]])
                vios.append(checklib.violation(
                    SCRIPT, "warning", v["at"], f"{side}.Cu", v["net"],
                    [f["ref"]],
                    f"{f['ref']} (Tag-Connect): via inside the plug's "
                    f"keep-out between the contact pads; a pogo pin or the "
                    f"plug's nose lands on it. Move the via out",
                    SOURCE, kind="tc_keepout_via", footprint=f["fpid"]))
        inner = area.buffer(-0.05)
        cu_layer = f"{side}.Cu"
        for t in tracks:
            if t["layer"] != cu_layer or inner.is_empty:
                continue
            if t["line"].intersects(inner):
                m = t["line"].intersection(inner)
                pos = m.centroid if not m.is_empty else t["line"].centroid
                fact["tracks"].append(t["net"])
                vios.append(checklib.violation(
                    SCRIPT, "warning", (pos.x, pos.y), cu_layer, t["net"],
                    [f["ref"]],
                    f"{f['ref']} (Tag-Connect): track on the contact side "
                    f"runs through the keep-out between the contact pads; "
                    f"route it round the footprint (Tag-Connect datasheet "
                    f"keep-out)", SOURCE, kind="tc_keepout_track",
                    footprint=f["fpid"]))
        for p in contacts:
            if p["paste"]:
                fact["paste"].append(p["number"])
        if fact["paste"]:
            vios.append(checklib.violation(
                SCRIPT, "warning", f["pos"], f"{side}.Paste", None,
                [f["ref"]],
                f"{f['ref']} (Tag-Connect): contact pad(s) "
                f"{', '.join(fact['paste'])} carry paste; solder domes on "
                f"the contacts and the pogo pins skate. Take the paste "
                f"layer off the pads", SOURCE, kind="tc_paste",
                footprint=f["fpid"], pads=fact["paste"]))
        facts.append(fact)
    return vios, facts


# ------------------------------------------------------------ main check

def _cfg(constraints: dict | None) -> dict:
    tp = (constraints or {}).get("testpoints") or {}
    if not isinstance(tp, dict):
        raise checklib.CheckError("constraints testpoints must be an object")
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in tp:
            cfg[k] = float(tp[k])
    cfg["nets"] = tp.get("nets")
    cfg["min_coverage_pct"] = tp.get("min_coverage_pct")
    cfg["fixture"] = bool(tp.get("fixture"))
    return cfg


def _point_fact(p: dict) -> dict:
    out = {"ref": p["ref"], "pad": p["pad"], "net": p["net"],
           "side": SIDE_NAME[p["side"]], "kind": p["kind"],
           "at": [checklib.rnd(c) for c in p["at"]],
           "size_mm": checklib.rnd(p["size"], 3),
           "flying_ok": p["flying_ok"], "fixture_ok": p["fixture_ok"],
           "fixture_fails": p["fixture_fails"]}
    for k in ("pitch_mm", "body_mm", "body_ref", "edge_mm"):
        if p.get(k) is not None:
            out[k] = p[k]
    if p["blocked"]:
        out["blocked"] = p["blocked"]
    return out


def check(pcb: Path, constraints: dict | None = None,
          all_points: bool = False) -> tuple[list[dict], dict]:
    cfg = _cfg(constraints)
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
    fps = read_board(root, dflt, pcb)
    try:
        outline = geom.BoardGeom(pcb, root).outline
    except Exception:  # noqa: BLE001 - no closed outline: skip edge rule
        outline = None
    pts, stats = access_points(fps, vias, cfg)
    cls = classify(pts, fps, outline, cfg)

    counts = board_nets(fps)
    nets = sorted(n for n, k in counts.items()
                  if k >= 2 and not n.startswith("unconnected-"))
    if cfg["nets"]:
        globs = cfg["nets"]
        nets = sorted(n for n in counts
                      if any(fnmatch.fnmatchcase(n, g) for g in globs))
    cov = coverage(pts, nets)
    flying = {p["net"] for p in pts if p["flying_ok"]}
    no_access = [n for n in nets if n not in flying]
    rails_blind = [n for n in no_access if is_power(n)]

    vios: list[dict] = []
    tps = [p for p in pts if p["kind"] == "tp"]
    for p in tps:
        if p["flying_ok"]:
            continue
        why = {"tented": "is covered by solder mask",
               "under_body": f"lies under {', '.join(p['under'])}",
               }.get(p["blocked"], f"leaves a {p['size']:.2f} mm probe "
                     f"target (flying probe needs {cfg['probe_mm']} mm)")
        vios.append(checklib.violation(
            SCRIPT, "warning", p["at"], f"{p['side']}.Cu", p["net"],
            [p["ref"]],
            f"{p['ref']} pad {p['pad']} ({p['net']}, "
            f"{SIDE_NAME[p['side']]}): the test point {why}; no probe can "
            f"use it", SOURCE, kind="tp_unusable", pad=p["pad"],
            side=SIDE_NAME[p["side"]], size_mm=checklib.rnd(p["size"], 3),
            reason=p["blocked"] or "small"))
    for n in rails_blind:
        vios.append(checklib.violation(
            SCRIPT, "warning", None, None, n, [],
            f"power net {n} has no probe-able access point (no test point, "
            f"open pad or untented via of {cfg['probe_mm']} mm or more on "
            f"either side): bring-up and test can't reach it. Add a test "
            f"point", SOURCE, kind="tp_rail_no_access"))
    if cfg["min_coverage_pct"] is not None and \
            cov["flying_either"] < float(cfg["min_coverage_pct"]) - EPS:
        vios.append(checklib.violation(
            SCRIPT, "warning", None, None, None, [],
            f"test-point coverage {cov['flying_either']}% of {len(nets)} "
            f"nets is under the {cfg['min_coverage_pct']}% asked "
            f"(constraints testpoints.min_coverage_pct); "
            f"{len(no_access)} nets have no access", SOURCE,
            kind="tp_coverage_low", coverage_pct=cov["flying_either"]))
    if cfg["fixture"]:
        for p in tps:
            if p["flying_ok"] and not p["fixture_ok"]:
                vios.append(checklib.violation(
                    SCRIPT, "warning", p["spot"], f"{p['side']}.Cu",
                    p["net"], [p["ref"]],
                    f"{p['ref']} pad {p['pad']} ({p['net']}): a fixture "
                    f"probe can't use it ({', '.join(p['fixture_fails'])}: "
                    f"needs {cfg['fixture_mm']} mm, {cfg['pitch_mm']} mm "
                    f"pitch, {cfg['body_mm']} mm or 0.3 x height to parts, "
                    f"{cfg['edge_mm']} mm from the edge)", SOURCE,
                    kind="tp_fixture", pad=p["pad"],
                    fails=p["fixture_fails"]))
    tv, tfacts = tag_connect(fps, root, vias)
    vios += tv

    seen = {
        "tenting_assumed": dflt["tenting_assumed"],
        "config": {k: cfg[k] for k in DEFAULTS},
        "nets": len(nets), "nets_scope": "constraints" if cfg["nets"]
        else "all nets with 2+ pads",
        "coverage_pct": cov,
        "nets_no_access": no_access,
        "power_nets_no_access": rails_blind,
        "access_points": {
            "total": len(pts),
            "flying_ok": sum(p["flying_ok"] for p in pts),
            "fixture_ok": sum(p["fixture_ok"] for p in pts),
            "by_kind": {k: sum(1 for p in pts if p["kind"] == k)
                        for k in sorted({p["kind"] for p in pts})},
            **stats},
        "testpoints": [_point_fact(p) for p in tps],
        "tag_connect": tfacts,
        "bodies": {k: sum(1 for f in fps if f["body_src"] == k)
                   for k in ("fab", "model", "unknown")},
        "body_unknown": sorted(f["ref"] for f in fps
                               if f["body_src"] == "unknown"),
        **cls,
    }
    if all_points:
        seen["points"] = [_point_fact(p) for p in pts]
    return vios, seen


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--all-points", action="store_true",
                    help="list every access point in the facts")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints and Path(args.constraints).is_file() else None
    vios, seen = check(pcb, cons, args.all_points)
    return checklib.report(
        SCRIPT, pcb, vios, checked=[seen],
        not_judged=[check_wicking.TENTING_ASSUMED]
        if seen.get("tenting_assumed") else None), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
