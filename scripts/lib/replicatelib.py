"""replicatelib - multi-channel layout reuse (#482): lay out channel 1, stamp
channels 2..N.

A board with N identical channels (one hierarchical sheet instanced N times,
or N copies of a sheet) gets ONE layout: the source channel's placement and
routing are copied into every target channel under a declared rigid
transform, so channel-to-channel matching is by construction, not by chance.
Pure Python (sexpdata + shapely); place_replicate.py applies the plan through
place_edit / route_edit, and place_metrics / route_quality report the
`channel_symmetry` this module measures.

constraints.json:

  "channels": [{
    "name": "afe",                       # optional label
    "source": "ch1" | ["R101", "C101", ...],
    "targets": ["ch2", "ch3"] | [["R201", ...], ["R301", ...]],
    "transform": "grid" | "explicit",
    "pitch": [dx, dy],                   # grid: target i at (i+1) * pitch
    "offsets": [{"dx": .., "dy": .., "rot": 0|90|180|270}, ...],
                                         # explicit: one per target
    "origin": [x, y],                    # rotation centre (default: the
                                         # centre of the source parts' bbox)
    "route": true,                       # copy tracks/vias too (default)
    "margin_mm": 1.0                     # region slack for shared nets
  }]

Identity. A sheet is named by its instance name ("ch2") or full path
("/ch2/"). Parts of two instances are paired by the symbol's own uuid (the
netlist comp's `(tstamps)`, the last element of a footprint `(path)`), which
is the same in every instance of a sheet file - so R101 pairs with R201
because they are the same symbol, not because the numbers line up. hwde's
boards carry no footprint paths (board_init imports refs and nets only), so
the identity comes from the netlist next to the board (`<board>.net`); a
board updated from the schematic in KiCad carries `(path)` + `(sheetname)`
and is read directly. Two instances with NO uuid in common (two copies of a
sheet file, hwde's own generator style) pair by reference suffix: the
number modulo 100 within each prefix (R101 <-> R201). A ref-list source
pairs with each ref-list target by position.

A target part with no source counterpart (an extra part in one channel's
sheet) is `replicate_unmatched`: it is not stamped and falls back to normal
placement - left where it is when that spot is legal, else moved to the
nearest free spot (no courtyard overlap, inside the outline, clear of the
stamped copper). A source part with no counterpart in a target, or a pair
whose footprints differ, is reported and not stamped either.

Routing. Nets map through the pad correspondence (source pad net -> target
pad net; a source net that would land on two target nets is ambiguous and
not copied). A source net whose pads all belong to the source channel is
internal: all its copper is copied. Any other net (GND, a rail, a net
leaving the channel) is shared: only its copper lying inside the source
channel's region (hull of the courtyards + margin) is copied, mapped through
the same pad table (GND maps to GND). A copied item that touches a pad of
another net, or another net's existing copper, is dropped
(`replicate_track_dropped`). Arcs and blind/buried vias are not copied
(route_edit adds straight tracks and through vias only) and are reported.
The target's own copper on its internal nets is ripped first: a stamp
replaces it. What is left (the nets between channels and the rest of the
board) is route_auto's.

KiCad 10's native hook (rule areas with a placement source, ZONE
SetPlacementAreaSourceType / PLACEMENT_SOURCE_T_SHEETNAME, pcbnew 10.0.6 has
it) only feeds the GUI's "Repeat Layout" action, which SWIG does not expose,
so the transform is done here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from shapely import affinity
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

import geom
import placelib

POS_TOL = 1e-3      # mm: "the same" placement / vertex (1 um)
ANG_TOL = 0.01      # deg
KINDS = ("replicate_unmatched", "replicate_missing", "replicate_fp_mismatch",
         "replicate_track_dropped", "replicate_not_copied",
         "replicate_net_ambiguous", "replicate_fallback_unplaced")


class ReplicateError(RuntimeError):
    pass


# ------------------------------------------------------------ identity

@dataclass(frozen=True)
class Ident:
    ref: str
    sheet: str          # "/ch2/" ("/" = root)
    sym: str            # symbol uuid ("" unknown)


def _sx(path: Path):
    import sexpdata
    return sexpdata.loads(Path(path).read_text(encoding="utf-8"))


def idents_from_netlist(net: Path) -> dict[str, Ident]:
    out: dict[str, Ident] = {}
    root = _sx(net)
    for sec in root[1:]:
        if geom._head(sec) != "components":
            continue
        for comp in geom._kids(sec, "comp"):
            ref = geom._strs(geom._kid(comp, "ref"))[0]
            sp = geom._kid(comp, "sheetpath")
            names = geom._kid(sp, "names") if sp is not None else None
            sheet = geom._strs(names)[0] if names is not None else "/"
            ts = geom._kid(comp, "tstamps")
            sym = geom._strs(ts)[0] if ts is not None and geom._strs(ts) else ""
            out[ref] = Ident(ref, sheet, sym)
    return out


def idents_from_board(pcb: Path) -> dict[str, Ident]:
    """Footprints with a hierarchical (path "/<sheet>/<symbol>") and a
    (sheetname); {} when the board has none (hwde's own boards)."""
    out: dict[str, Ident] = {}
    for fp in geom._kids(_sx(pcb), "footprint"):
        ref = None
        for prop in geom._kids(fp, "property"):
            s = geom._strs(prop)
            if len(s) >= 2 and s[0] == "Reference":
                ref = s[1]
        p = geom._kid(fp, "path")
        if not ref or p is None or not geom._strs(p):
            continue
        parts = [x for x in geom._strs(p)[0].split("/") if x]
        sn = geom._kid(fp, "sheetname")
        name = geom._strs(sn)[0] if sn is not None and geom._strs(sn) else ""
        # KiCad writes the human path ("/ch2/", "/" on the root); older
        # boards a bare name ("ch2")
        name = name.strip("/")
        sheet = f"/{name}/" if name else ("/" if len(parts) <= 1
                                          else "/" + "/".join(parts[:-1]) + "/")
        out[ref] = Ident(ref, sheet, parts[-1] if parts else "")
    return out


def load_idents(pcb: Path, netlist: Path | None) -> tuple[dict[str, Ident], str]:
    if netlist is None:
        cand = Path(pcb).with_suffix(".net")
        netlist = cand if cand.is_file() else None
    board = idents_from_board(Path(pcb))
    if any(i.sheet != "/" for i in board.values()):
        return board, "board"
    if netlist is not None and Path(netlist).is_file():
        return idents_from_netlist(Path(netlist)), str(netlist)
    return board, "board"


def _sheet_refs(idents: dict[str, Ident], name: str) -> list[str]:
    want = name if name.startswith("/") else None
    out = []
    for r, i in idents.items():
        if want is not None:
            if i.sheet == (want if want.endswith("/") else want + "/"):
                out.append(r)
        elif i.sheet.rstrip("/").split("/")[-1] == name:
            out.append(r)
    return sorted(out)


_REF = re.compile(r"^([A-Za-z#_]+)(\d+)$")


def _suffix_key(ref: str):
    m = _REF.match(ref)
    return (m.group(1), int(m.group(2)) % 100) if m else (ref, None)


def pair_refs(src: list[str], tgt: list[str], idents: dict[str, Ident],
              by_position: bool) -> tuple[dict[str, str], list[str], list[str], str]:
    """-> (source ref -> target ref, unmatched target refs, missing source
    refs, how)."""
    if by_position:
        n = min(len(src), len(tgt))
        return (dict(zip(src[:n], tgt[:n])), tgt[n:], src[n:], "position")
    how = "symbol_uuid"
    key = {r: idents[r].sym for r in src + tgt if r in idents}
    if not ({key.get(r) for r in src} & {key.get(r) for r in tgt}) - {"", None}:
        how = "ref_suffix"
        key = {r: _suffix_key(r) for r in src + tgt}
    tk: dict = {}
    for r in tgt:
        tk.setdefault(key.get(r), []).append(r)
    m: dict[str, str] = {}
    missing = []
    for r in src:
        cands = tk.get(key.get(r)) or []
        if len(cands) == 1 and key.get(r) not in ("", None):
            m[r] = cands[0]
        else:
            missing.append(r)
    taken = set(m.values())
    return m, sorted(r for r in tgt if r not in taken), sorted(missing), how


# ------------------------------------------------------------ transform

@dataclass(frozen=True)
class Xform:
    dx: float
    dy: float
    rot: float          # KiCad degrees (a footprint's angle adds this)
    origin: tuple[float, float]

    def pt(self, p) -> tuple[float, float]:
        ox, oy = self.origin
        x, y = geom._rot(p[0] - ox, p[1] - oy, -self.rot)
        return (round(ox + x + self.dx, 6), round(oy + y + self.dy, 6))

    def ang(self, a: float) -> float:
        return round((a + self.rot) % 360.0, 6)

    def shape(self, g):
        if self.rot:
            g = affinity.rotate(g, -self.rot, origin=self.origin)
        return affinity.translate(g, self.dx, self.dy)

    def as_dict(self) -> dict:
        return {"dx": self.dx, "dy": self.dy, "rot": self.rot,
                "origin": list(self.origin)}


def transforms(ch: dict, n_targets: int, origin) -> list[Xform]:
    kind = ch.get("transform", "grid")
    if kind == "grid":
        p = ch.get("pitch")
        if not (isinstance(p, list) and len(p) == 2):
            raise ReplicateError(f"channel {ch.get('name', '?')}: grid needs "
                                 "pitch [dx, dy]")
        return [Xform(p[0] * (i + 1), p[1] * (i + 1), 0.0, origin)
                for i in range(n_targets)]
    if kind == "explicit":
        offs = ch.get("offsets") or []
        if len(offs) != n_targets:
            raise ReplicateError(f"channel {ch.get('name', '?')}: explicit "
                                 f"needs {n_targets} offsets, got {len(offs)}")
        out = []
        for o in offs:
            rot = float(o.get("rot", 0)) % 360.0
            if rot not in (0.0, 90.0, 180.0, 270.0):
                raise ReplicateError(f"rot {rot}: 0/90/180/270 only")
            out.append(Xform(float(o.get("dx", 0)), float(o.get("dy", 0)),
                             rot, origin))
        return out
    raise ReplicateError(f"transform '{kind}': grid or explicit")


# ------------------------------------------------------------ the plan

@dataclass
class Target:
    label: str
    xf: Xform
    pairs: dict[str, str]
    unmatched: list[str]
    missing: list[str]
    how: str
    netmap: dict[str, str] = field(default_factory=dict)


@dataclass
class Channel:
    name: str
    source: list[str]
    targets: list[Target]
    route: bool
    margin: float


def resolve(constraints: dict, idents: dict[str, Ident],
            model: placelib.PlaceModel) -> list[Channel]:
    out = []
    for k, ch in enumerate(constraints.get("channels") or []):
        name = ch.get("name") or f"channel{k + 1}"
        src_spec, tgt_specs = ch.get("source"), ch.get("targets") or []
        by_pos = isinstance(src_spec, list)
        src = (list(src_spec) if by_pos else _sheet_refs(idents, str(src_spec)))
        src = [r for r in src if r in model.footprints]
        if not src:
            raise ReplicateError(f"channel {name}: source {src_spec!r} has no "
                                 "parts on the board")
        xs = [model.footprints[r].pos[0] for r in src]
        ys = [model.footprints[r].pos[1] for r in src]
        origin = tuple(ch.get("origin") or ((min(xs) + max(xs)) / 2,
                                            (min(ys) + max(ys)) / 2))
        xfs = transforms(ch, len(tgt_specs), origin)
        targets = []
        for spec, xf in zip(tgt_specs, xfs):
            tgt = list(spec) if by_pos else _sheet_refs(idents, str(spec))
            tgt = [r for r in tgt if r in model.footprints]
            pairs, unm, miss, how = pair_refs(src, tgt, idents, by_pos)
            targets.append(Target(str(spec) if not by_pos else ",".join(spec[:2]) + "..",
                                  xf, pairs, unm, miss, how))
        out.append(Channel(name, src, targets, ch.get("route", True),
                           float(ch.get("margin_mm", 1.0))))
    return out


def _viol(kind, sev, pos, net, refs, msg):
    import checklib
    return checklib.violation("place_replicate", sev, pos, None, net, refs,
                             msg, "place_replicate", kind=kind)


def _netmaps(bg: geom.BoardGeom, ch: Channel, t: Target):
    by_ref: dict[str, dict[str, str]] = {}
    for p in bg.pads_of():
        if p.net:
            by_ref.setdefault(p.ref, {})[p.number] = p.net
    seen: dict[str, set] = {}
    for s, d in t.pairs.items():
        tp = by_ref.get(d, {})
        for num, net in by_ref.get(s, {}).items():
            tn = tp.get(num)
            if tn:
                seen.setdefault(net, set()).add(tn)
    good = {n: next(iter(v)) for n, v in seen.items() if len(v) == 1}
    bad = sorted(n for n, v in seen.items() if len(v) > 1)
    return good, bad


def plan(pcb: Path, constraints: dict, netlist: Path | None = None,
         stage: str = "all") -> dict:
    """The ops + report. stage: place | route | all."""
    pcb = Path(pcb)
    model = placelib.PlaceModel(pcb)
    bg = geom.BoardGeom.from_file(pcb)
    idents, id_src = load_idents(pcb, netlist)
    channels = resolve(constraints, idents, model)
    place_ops: list[dict] = []
    route_ops: list[dict] = []
    viol: list[dict] = []
    report = {"identity": id_src, "channels": []}
    stamped_shapes = []          # courtyards of every stamped target part
    stamped_copper = []
    fallback: list[tuple[str, Target]] = []

    for ch in channels:
        crep = {"name": ch.name, "source": ch.source, "targets": []}
        src_shapes = [model.footprints[r].extents_abs() for r in ch.source]
        region = unary_union(src_shapes).convex_hull.buffer(ch.margin)
        src_pads = {r for r in ch.source}
        net_pads: dict[str, set] = {}
        for p in bg.pads_of():
            if p.net:
                net_pads.setdefault(p.net, set()).add(p.ref)
        for t in ch.targets:
            trep = {"target": t.label, "map_by": t.how, "pairs": len(t.pairs),
                    "transform": t.xf.as_dict(), "unmatched": t.unmatched,
                    "missing": t.missing}
            for r in t.unmatched:
                viol.append(_viol("replicate_unmatched", "warning",
                                  model.footprints[r].pos, None, [r],
                                  f"{r} in {t.label} has no counterpart in "
                                  f"{ch.name}'s source: not stamped, normal "
                                  "placement"))
                fallback.append((r, t))
            for r in t.missing:
                viol.append(_viol("replicate_missing", "warning",
                                  model.footprints[r].pos, None, [r],
                                  f"source {r} has no counterpart in "
                                  f"{t.label}"))
            for s, d in sorted(t.pairs.items()):
                fs, fd = model.footprints[s], model.footprints[d]
                if fs.fpid != fd.fpid:
                    viol.append(_viol("replicate_fp_mismatch", "warning",
                                      fd.pos, None, [s, d],
                                      f"{s} is {fs.fpid}, {d} is {fd.fpid}: "
                                      "not stamped"))
                    fallback.append((d, t))
                    continue
                x, y = t.xf.pt(fs.pos)
                place_ops.append({"op": "place", "ref": d, "x": x, "y": y,
                                  "deg": t.xf.ang(fs.angle), "side": fs.side})
                stamped_shapes.append(t.xf.shape(fs.extents_abs()))
            good, bad = _netmaps(bg, ch, t)
            t.netmap = good
            for n in bad:
                viol.append(_viol("replicate_net_ambiguous", "warning", None,
                                  n, [], f"{n} lands on more than one net in "
                                  f"{t.label}: its copper is not copied"))
            if ch.route and stage in ("route", "all"):
                rops, rv, copper, rrep = _route_plan(bg, ch, t, region,
                                                     net_pads, src_pads)
                route_ops.extend(rops)
                viol.extend(rv)
                stamped_copper.extend(copper)
                trep["routing"] = rrep
            crep["targets"].append(trep)
        report["channels"].append(crep)

    # fallback placement for unmatched parts
    stamped = {op["ref"] for op in place_ops}
    fb_ops = _fallback(model, [r for r, _ in fallback if r not in stamped],
                       stamped_shapes, stamped_copper, stamped, viol)
    if stage == "route":
        place_ops, fb_ops = [], []
    report["fallback"] = [{"ref": op["ref"], "x": op["x"], "y": op["y"]}
                          for op in fb_ops]
    report["fallback_refs"] = sorted({r for r, _ in fallback})
    return {"place_ops": place_ops + fb_ops, "route_ops": route_ops,
            "violations": viol, "report": report, "channels": channels}


def _straight(t: geom.Track) -> bool:
    return len(t.shape.coords) == 2


def _route_plan(bg, ch: Channel, t: Target, region, net_pads, src_refs):
    ops, viol, copper = [], [], []
    rep = {"tracks": 0, "vias": 0, "dropped": 0, "not_copied": 0,
           "ripped": 0, "nets": 0}
    src = set(src_refs)
    tgt_refs = set(t.pairs.values()) | set(t.unmatched)
    internal = {n for n, refs in net_pads.items() if refs <= src}
    # target pads at their STAMPED position (transformed source pads), every
    # other pad where it is; the target's unmatched parts are ignored (they
    # move out of the way afterwards).
    obstacles = []
    pad_net = {(q.ref, q.number): q.net for q in bg.pads_of()}
    for p in bg.pads_of():
        if p.ref in tgt_refs:
            continue
        if p.ref in t.pairs:
            tn = pad_net.get((t.pairs[p.ref], p.number))
            obstacles.append((t.xf.shape(p.poly), tn, set(p.layers)))
            obstacles.append((p.poly, p.net, set(p.layers)))
        else:
            obstacles.append((p.poly, p.net, set(p.layers)))
    tgt_internal = {t.netmap[n] for n in internal if n in t.netmap}
    keep, rippable = [], []
    for tr in bg.tracks_of():
        if tr.net in tgt_internal:
            rippable.append(tr)
        else:
            keep.append((tr.poly, tr.net, {tr.layer}))
    for v in bg.vias_of():
        if v.net in tgt_internal:
            rippable.append(v)
        else:
            keep.append((v.poly, v.net, set(v.layers)))

    from shapely.strtree import STRtree
    items = obstacles + keep
    tree = STRtree([g for g, _, _ in items])

    def clash(shape, net, layers):
        for i in sorted(tree.query(shape)):
            g, n, ls = items[i]
            if n != net and (ls & layers) and g.intersects(shape):
                return n or "(no net)"
        return None

    nets = set()
    for tr in bg.tracks_of():
        if tr.net not in t.netmap:
            continue
        if tr.net not in internal and not region.contains(tr.shape):
            continue
        if not _straight(tr):
            rep["not_copied"] += 1
            viol.append(_viol("replicate_not_copied", "warning",
                              tr.shape.coords[0], tr.net, [],
                              f"arc on {tr.net}: route_edit adds straight "
                              "tracks only"))
            continue
        net = t.netmap[tr.net]
        a, b = t.xf.pt(tr.shape.coords[0]), t.xf.pt(tr.shape.coords[-1])
        shape = LineString([a, b]).buffer(tr.width / 2.0)
        hit = clash(shape, net, {tr.layer})
        if hit:
            rep["dropped"] += 1
            viol.append(_viol("replicate_track_dropped", "warning", a, net, [],
                              f"copied {tr.layer} track of {net} would touch "
                              f"{hit}: dropped"))
            continue
        ops.append({"op": "add_track", "start": list(a), "end": list(b),
                    "width": tr.width, "layer": tr.layer, "net": net})
        copper.append(shape)
        rep["tracks"] += 1
        nets.add(net)
    for v in bg.vias_of():
        if v.net not in t.netmap:
            continue
        if v.net not in internal and not region.contains(Point(v.at)):
            continue
        if v.kind != "through" or tuple(v.layers) != tuple(bg.copper_layers):
            rep["not_copied"] += 1
            viol.append(_viol("replicate_not_copied", "warning", v.at, v.net,
                              [], f"{v.kind} via on {v.net}: route_edit adds "
                              "through vias only"))
            continue
        net = t.netmap[v.net]
        at = t.xf.pt(v.at)
        shape = Point(at).buffer(v.diameter / 2.0)
        hit = clash(shape, net, set(v.layers))
        if hit:
            rep["dropped"] += 1
            viol.append(_viol("replicate_track_dropped", "warning", at, net,
                              [], f"copied via of {net} would touch {hit}: "
                              "dropped"))
            continue
        ops.append({"op": "add_via", "at": list(at), "size": v.diameter,
                    "drill": v.drill, "net": net})
        copper.append(shape)
        rep["vias"] += 1
        nets.add(net)
    # the target's own copper on its internal nets: an item the stamp would
    # add anyway stays (a re-run is a no-op), the rest is ripped
    for it in rippable:
        same = None
        for k, op in enumerate(ops):
            if isinstance(it, geom.Via):
                if op["op"] == "add_via" and op["net"] == it.net \
                        and _near(op["at"], it.at):
                    same = k
                    break
            elif op["op"] == "add_track" and op["net"] == it.net \
                    and op["layer"] == it.layer and _straight(it):
                c = it.shape.coords
                if (_near(c[0], op["start"]) and _near(c[-1], op["end"])) or \
                        (_near(c[0], op["end"]) and _near(c[-1], op["start"])):
                    same = k
                    break
        if same is not None:
            ops.pop(same)
            rep["kept"] = rep.get("kept", 0) + 1
        elif it.uuid:
            ops.append({"op": "remove", "uuid": it.uuid})
            rep["ripped"] += 1
    rep["nets"] = len(nets)
    return ops, viol, copper, rep


FALLBACK_GAP = 2.0   # mm: courtyard slack: a refdes stands ~1.4 mm off its part
FALLBACK_REACH = 25.0  # mm: how far the fallback looks for a free spot


def _fallback(model, refs, stamped_shapes, copper, stamped, viol):
    """Normal placement for the parts the stamp skipped: keep the spot when
    it's legal, else the nearest free spot (0.5 mm spiral out to
    FALLBACK_REACH, same angle)."""
    from shapely.prepared import prep
    ops = []
    if not refs:
        return ops
    inner = prep(model.outline.buffer(-FALLBACK_GAP / 2))
    fixed = [f.extents_abs() for r, f in model.footprints.items()
             if r not in stamped and r not in refs]
    placed = list(stamped_shapes) + fixed + list(copper)
    for r in sorted(refs):
        f = model.footprints[r]
        shape = f.extents_abs()
        halo = shape.buffer(FALLBACK_GAP)
        busy = prep(unary_union(placed)) if placed else None

        def free(dx, dy):
            g = affinity.translate(shape, dx, dy) if (dx or dy) else shape
            if not inner.contains(g):
                return None
            h = affinity.translate(halo, dx, dy) if (dx or dy) else halo
            return None if busy is not None and busy.intersects(h) else g
        found = (0.0, 0.0, shape) if free(0.0, 0.0) is not None else None
        ring = 0
        while found is None and ring < FALLBACK_REACH / 0.5:
            ring += 1
            step = 0.5 * ring
            for k in range(8 * ring):
                ang = 2 * math.pi * k / (8 * ring)
                dx, dy = (round(step * math.cos(ang), 3),
                          round(step * math.sin(ang), 3))
                g = free(dx, dy)
                if g is not None:
                    found = (dx, dy, g)
                    break
        if found is None:
            viol.append(_viol("replicate_fallback_unplaced", "error", f.pos,
                              None, [r], f"{r}: no free spot within "
                              f"{FALLBACK_REACH} mm for the unstamped part"))
            continue
        dx, dy, g = found
        placed.append(g)
        if dx or dy:
            ops.append({"op": "move", "ref": r,
                        "x": round(f.pos[0] + dx, 4),
                        "y": round(f.pos[1] + dy, 4)})
    return ops


# ------------------------------------------------------------ symmetry

def symmetry(pcb: Path, constraints: dict, netlist: Path | None = None,
             routing: bool = True) -> dict | None:
    """How far each target channel is from its source under the declared
    transform. None when constraints declare no channels.

      placement   fraction of paired parts at T(source) within 1 um and
                  the same angle and side
      routing     fraction of the source's copied copper (length, vias as
                  1 mm) found in the target at T(source) within 1 um, on
                  the mapped net
      score       min over targets of placement (x routing when asked)
    """
    if not constraints.get("channels"):
        return None
    pcb = Path(pcb)
    model = placelib.PlaceModel(pcb)
    bg = geom.BoardGeom.from_file(pcb)
    idents, _ = load_idents(pcb, netlist)
    chans = resolve(constraints, idents, model)
    out = {"channels": [], "score": 1.0}
    for ch in chans:
        region = unary_union([model.footprints[r].extents_abs()
                              for r in ch.source]).convex_hull.buffer(ch.margin)
        net_pads: dict[str, set] = {}
        for p in bg.pads_of():
            if p.net:
                net_pads.setdefault(p.net, set()).add(p.ref)
        internal = {n for n, refs in net_pads.items() if refs <= set(ch.source)}
        crow = {"name": ch.name, "targets": []}
        for t in ch.targets:
            good, _ = _netmaps(bg, ch, t)
            t.netmap = good
            ok = 0
            dev = 0.0
            for s, d in t.pairs.items():
                fs, fd = model.footprints[s], model.footprints[d]
                x, y = t.xf.pt(fs.pos)
                e = math.hypot(fd.pos[0] - x, fd.pos[1] - y)
                dev = max(dev, e)
                da = abs((fd.angle - t.xf.ang(fs.angle) + 180) % 360 - 180)
                if e <= POS_TOL and da <= ANG_TOL and fd.side == fs.side:
                    ok += 1
            n = len(t.pairs) + len(t.missing)
            row = {"target": t.label, "placement": round(ok / n, 4) if n else 1.0,
                   "max_dev_mm": round(dev, 4), "unmatched": t.unmatched}
            score = row["placement"]
            if routing:
                want = got = 0.0
                tsegs: dict[tuple, list] = {}
                for tr in bg.tracks_of():
                    tsegs.setdefault((tr.layer, tr.net), []).append(tr.shape)
                tvias: dict[str, list] = {}
                for v in bg.vias_of():
                    tvias.setdefault(v.net, []).append(v.at)
                for tr in bg.tracks_of():
                    if tr.net not in good or not _straight(tr):
                        continue
                    if tr.net not in internal and not region.contains(tr.shape):
                        continue
                    want += tr.length
                    a = t.xf.pt(tr.shape.coords[0])
                    b = t.xf.pt(tr.shape.coords[-1])
                    for sh in tsegs.get((tr.layer, good[tr.net]), ()):
                        c = sh.coords
                        if len(c) == 2 and (
                                (_near(c[0], a) and _near(c[1], b)) or
                                (_near(c[0], b) and _near(c[1], a))):
                            got += tr.length
                            break
                for v in bg.vias_of():
                    if v.net not in good:
                        continue
                    if v.net not in internal and not region.contains(Point(v.at)):
                        continue
                    want += 1.0
                    at = t.xf.pt(v.at)
                    if any(_near(p, at) for p in tvias.get(good[v.net], ())):
                        got += 1.0
                row["routing"] = round(got / want, 4) if want else None
                if want:
                    score = min(score, row["routing"])
            row["score"] = round(score, 4)
            out["score"] = min(out["score"], row["score"])
            crow["targets"].append(row)
        out["channels"].append(crow)
    out["score"] = round(out["score"], 4)
    return out


def _near(p, q) -> bool:
    return abs(p[0] - q[0]) <= POS_TOL and abs(p[1] - q[1]) <= POS_TOL
