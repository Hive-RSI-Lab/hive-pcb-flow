"""route_edit - apply an atomic track/via op list (S11, SPEC P7/section 4).

The pipeline's ONLY writer for routing changes outside route_auto's SES import
(SPEC section 4: op lists applied atomically; agents never raw-edit the
board). Generators (stitch_vias.py, plane_repair.py, route_cleanup.py, fixer
agents) emit ops; this driver applies them via the SWIG bundled python worker
(lib/route_swig.py) - the same V4/V8 edit-path decision as place_edit (S9).

Contract:
  route_edit.py --pcb B.kicad_pcb --ops ops.json [--out-report r.json]
  ops.json: {"version": 1, "ops": [...]}  (op shapes: see lib/route_swig.py)
    add_track {start:[x,y], end:[x,y], width, layer, net}
    add_via   {at:[x,y], size, drill, net}          # through via
              [kind: micro|blind|buried, top, bot]  # a span (#477)
              [teardrops: {length_ratio, max_length_mm, width_ratio,
                           max_width_mm, filter_ratio, curved, two_tracks,
                           prefer_zones}]   # enabled with these (#1138); a
                                            # key left out takes the default
    remove    {uuid}                         # track, via or zone; absent=noop
    set_net   {uuid, net}                            # worker-level (renet)
    set_via_fill {uuid, filled}                      # filling + capping
    set_point {uuid, which: start|end|at, to}  # a straight track's start
                                     # or end, or a via's centre (at), goes to
                                     # `to` and the uuid stays - part_repair
                                     # cuts a dead tail back with it (#1140)
    set_width {uuid, width}          # the worker's, emitted by the driver only

Selector ops - the ECOs boards used to hand-script with pcbnew (U-ECO; the
hi-fi boards carried ~45 rip/renet/add scripts because route_edit only took
uuids). The driver resolves each against the parsed board (geom) into the
worker ops above, so they share the atomic apply + verify:
    rip      {SELECTOR}                    remove every track/via it selects
    renet    {to, SELECTOR}                move the selected tracks/vias to net `to`
                                           (copper that touches no pad: KiCad
                                           re-derives pad-tied copper's net
                                           from the pad on save, and verify
                                           refuses that)
    add_path {points:[[x,y],...], width, layer, net}   a polyline of add_tracks
    rename_net {from, to}                  board-wide: pads, tracks, vias and
                                           zones of `from` go to `to` (created
                                           when absent) - the routed rename
                                           board_update refuses
    set_width {width, SELECTOR}           the selected tracks (vias never)
                                           get this width, in place (uuid
                                           and arcs kept) - widen.py
    move_via {at, to, [net], [drag]}      the via at `at` moves to `to`;
                                           drag (default true) takes the
                                           straight same-net track ends on
                                           its centre along - move_via.py
    join_end {at, [net], [layer], [radius]}   a track's free end at `at`
                                           gets a track (same width) to the
                                           nearest same-net anchor on its
                                           layer - track end, via, pad
                                           centre - within radius (0.6 mm)
                                           - fix_dangle.py
    fill_vias {SELECTOR, [filled]}        the selected vias get filling and
                                           capping set (IPC-4761 Type VII,
                                           filled + capped) per via, or
                                           cleared with filled: false - so
                                           check_wicking and fab_export's
                                           via_fill_facts read them filled;
                                           pads: ["U301"] picks U301's
                                           via-in-pad
    copy_from {pcb, SELECTOR}             adds the tracks/vias the selector
                                           picks on ANOTHER board (an older
                                           revision); after a rip of the
                                           same selector it puts a net's
                                           routing back - set5v.py
    route {net, from, to, [width], [layers], [grid]}   a new route
                                           from END to END on route_finish's
                                           grid and rules (lib/mazeroute.py);
                                           END is "REF.PAD", "REF" (its pads
                                           on net), "x,y@Layer", "x,y", or
                                           several joined by "|" - pr.py
                                           (#926). layers default to the
                                           routable ones, grid to 0.05 mm
    route_group {jobs: [{net, from, to, [width]}], [iterations], [layers],
                 [grid]}                   every job routed together
                                           (PathFinder: cells two nets want
                                           cost more each round until none
                                           is shared), for a channel greedy
                                           order can't fill - pf.py (#928)
    reroute {net, region, [from, to], [width], [layers], [grid], [keep]}
                                           rips other signal nets' tracks
                                           crossing the box (and their vias
                                           in it), routes net (from -> to,
                                           or joining its islands), routes
                                           each ripped net back, trying
                                           other orders; refused unless no
                                           ripped net ends with more copper
                                           islands than it had (and net
                                           with fewer) - rr.py/grp.py (#927).
                                           Never ripped: power and pour
                                           nets, nets with a width rule,
                                           keep's, copper wider than its
                                           net's width
  Every op in one list is resolved against the board as it was before the
  list ran, so an op can't select what an earlier op in it added or moved.
  The routing ops are the exception: each routes around what the ops before
  it removed and added (not what they moved: set_point, move_via).
  SELECTOR (all given keys must match; at least one of net, pads, at,
  region, uuid_prefix):
    net: NAME | [NAME...]       only these nets
    layer: L                    tracks on L only (vias are never selected)
    kind: track|via|any         default any
    region: [x0,y0,x1,y1]       an end (via: its centre) inside the box
    at: [x,y], radius (0.02)    an end (via: its centre) within radius;
      touch: "any"              ... or any point of the track's centreline
    pads: ["REF" | "REF.PAD"], radius (0)   an end within radius of one of
                                those pads' copper, on the pad's own net
    uuid_prefix: S | [S...]     the item's uuid starts with one of these
  A selector that matches nothing is "absent" (a no-op, like remove), so a
  re-run is idempotent. Items without a uuid (synthetic boards) can't be
  selected.

  --op '{"op": "rip", "net": "VBUS"}' (repeatable) takes ops inline instead
  of --ops FILE; --refill refills zones on the staged copy (with the board's
  .kicad_pro/.kicad_dru beside it) before the swap.

Atomic + rollback (place_edit pattern): ops are validated (schema + nets/
layers against the board); the board is copied to a scratch dir INSIDE the
board's directory; the worker edits and saves the copy; the driver re-parses
the copy (geom) and verifies every add landed (endpoints/center 1e-3 mm,
width 1e-3) and every removed uuid is gone from the file text; only then does
os.replace() swap the .kicad_pcb in (same volume -> atomic; sibling project
files never touched). Any failure leaves the original board byte-identical.
Re-applying the same op list is idempotent: identical existing items are
skipped ("exists"), absent removal uuids are no-ops ("absent").

Zone fills are NOT refreshed here: adding/removing copper stales any pour it
crosses. Callers that touch plane layers must refill afterwards
(kicad-cli pcb drc --refill-zones --save-board; kc.run_drc(refill=True)).
exit 0 applied+verified / 2 error (rolled back).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import routelib  # noqa: E402
import teardroplib as tdl  # noqa: E402
from checklib import CheckError  # noqa: E402

OP_FIELDS = {  # op -> (required, optional)
    "add_track": ({"start", "end", "width", "layer", "net"}, set()),
    "add_via": ({"at", "size", "drill", "net"},
                {"kind", "top", "bot", "teardrops"}),
    "remove": ({"uuid"}, set()),
    "set_net": ({"uuid", "net"}, set()),
    "set_via_fill": ({"uuid", "filled"}, set()),
    "set_point": ({"uuid", "which", "to"}, set()),
}
SELECT_KEYS = {"net", "layer", "kind", "region", "at", "radius", "touch",
               "pads", "uuid_prefix"}
SCOPE_KEYS = {"net", "pads", "at", "region", "uuid_prefix"}
ECO_FIELDS = {  # selector ops, resolved by the driver into OP_FIELDS ops
    "rip": (set(), SELECT_KEYS),
    "renet": ({"to"}, SELECT_KEYS),
    "add_path": ({"points", "width", "layer", "net"}, set()),
    "rename_net": ({"from", "to"}, set()),
    "set_width": ({"width"}, SELECT_KEYS),
    "move_via": ({"at", "to"}, {"net", "drag"}),
    "join_end": ({"at"}, {"net", "layer", "radius"}),
    "copy_from": ({"pcb"}, SELECT_KEYS),
    "fill_vias": (set(), SELECT_KEYS | {"filled"}),
    "route": ({"net", "from", "to"}, {"width", "layers", "grid"}),
    "route_group": ({"jobs"}, {"layers", "grid", "iterations"}),
    "reroute": ({"net", "region"}, {"from", "to", "width", "layers",
                                    "grid", "keep"}),
}
ROUTE_OPS = ("route", "route_group", "reroute")
JOB_KEYS = {"net", "from", "to", "width"}
SELECTOR_OPS = ("rip", "renet", "set_width", "copy_from", "fill_vias")
AT_TOL = 0.02    # mm: default `at` radius (the hand scripts' 20000 nm)
JOIN_RADIUS = 0.6  # mm: join_end's default reach (fix_dangle.py's)
POS_TOL = 1e-3   # mm


def _num(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _pt(v) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and _num(v[0]) and _num(v[1]))


def validate_ops(doc: dict) -> list[dict]:
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise CheckError("ops file must be {'version': 1, 'ops': [...]}")
    ops = doc.get("ops")
    if not isinstance(ops, list) or not ops:
        raise CheckError("ops list is empty")
    for i, op in enumerate(ops):
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in OP_FIELDS and kind not in ECO_FIELDS:
            raise CheckError(f"ops[{i}]: unknown op '{kind}'")
        req, opt = OP_FIELDS.get(kind) or ECO_FIELDS[kind]
        missing = req - op.keys()
        if missing:
            raise CheckError(f"ops[{i}] ({kind}): missing {sorted(missing)}")
        unknown = op.keys() - req - opt - {"op"}
        if unknown:
            raise CheckError(f"ops[{i}] ({kind}): unknown keys {sorted(unknown)}")
        if kind in ROUTE_OPS:
            _validate_route(i, op)
        to_pt = kind in ("move_via", "set_point")
        for k in ("start", "end", "at") + (("to",) if to_pt else ()):
            if k in op and not _pt(op[k]):
                raise CheckError(f"ops[{i}]: {k} must be [x, y] mm")
        if kind == "set_point" and op["which"] not in ("start", "end", "at"):
            raise CheckError(f"ops[{i}]: which must be start, end or at")
        for k in ("width", "size", "drill"):
            if k in op and not (_num(op[k]) and op[k] > 0):
                raise CheckError(f"ops[{i}]: {k} must be a positive number")
        if kind == "add_via" and op["drill"] >= op["size"]:
            raise CheckError(f"ops[{i}]: drill must be < size")
        if kind == "add_via" and "kind" in op:
            if op["kind"] not in ("through", "micro", "blind", "buried"):
                raise CheckError(f"ops[{i}]: unknown via kind {op['kind']}")
            if op["kind"] != "through" and not ("top" in op and "bot" in op):
                raise CheckError(f"ops[{i}]: a {op['kind']} via needs top and bot")
        if kind == "add_via" and "teardrops" in op:
            _validate_teardrops(i, op["teardrops"])
        if "uuid" in op and not (isinstance(op["uuid"], str) and op["uuid"]):
            raise CheckError(f"ops[{i}]: uuid must be a non-empty string")
        for k in ("layer", "from", "pcb") + (("to",) if not to_pt else ()) \
                + (("net",) if kind not in SELECTOR_OPS else ()):
            if k in op and not (isinstance(op[k], str) and op[k]):
                raise CheckError(f"ops[{i}]: {k} must be a non-empty string")
        if kind in SELECTOR_OPS:
            _validate_selector(i, op)
        if kind == "set_width" and op.get("kind", "track") != "track":
            raise CheckError(f"ops[{i}]: set_width selects tracks only")
        if kind == "fill_vias" and op.get("kind", "via") != "via":
            raise CheckError(f"ops[{i}]: fill_vias selects vias only")
        if "filled" in op and not isinstance(op["filled"], bool):
            raise CheckError(f"ops[{i}]: filled must be true or false")
        if "drag" in op and not isinstance(op["drag"], bool):
            raise CheckError(f"ops[{i}]: drag must be true or false")
        if kind == "join_end" and "radius" in op \
                and not (_num(op["radius"]) and op["radius"] > 0):
            raise CheckError(f"ops[{i}]: radius must be > 0")
        if kind == "add_path":
            pts = op["points"]
            if not (isinstance(pts, list) and len(pts) >= 2
                    and all(_pt(p) for p in pts)):
                raise CheckError(f"ops[{i}]: points must be 2+ [x, y] mm")
        if kind == "rename_net" and op["from"] == op["to"]:
            raise CheckError(f"ops[{i}]: from and to are the same net")
    return ops


def _validate_teardrops(i: int, td) -> None:
    """add_via's teardrops (#1138): teardroplib.ITEM_PARAMS names, a flag
    true or false, a ratio or size a positive number."""
    if not isinstance(td, dict):
        raise CheckError(f"ops[{i}]: teardrops must be an object of "
                         f"{sorted(tdl.ITEM_PARAMS)}")
    unknown = td.keys() - tdl.ITEM_PARAMS.keys()
    if unknown:
        raise CheckError(f"ops[{i}]: teardrops: unknown keys "
                         f"{sorted(unknown)}")
    for k, v in td.items():
        if isinstance(tdl.ITEM_PARAMS[k], bool):
            if not isinstance(v, bool):
                raise CheckError(f"ops[{i}]: teardrops.{k} must be true or "
                                 "false")
        elif isinstance(v, bool) or not (_num(v) and v > 0):
            raise CheckError(f"ops[{i}]: teardrops.{k} must be a positive "
                             "number")


def _validate_route(i: int, op: dict) -> None:
    kind = op["op"]
    if "layers" in op and _strs(op["layers"]) is None:
        raise CheckError(f"ops[{i}]: layers must be a layer or a list of "
                         "layers")
    if "grid" in op and not (_num(op["grid"]) and 0.005 <= op["grid"] <= 0.5):
        raise CheckError(f"ops[{i}]: grid must be 0.005 to 0.5 mm")
    if "iterations" in op and not (isinstance(op["iterations"], int)
                                   and 1 <= op["iterations"] <= 200):
        raise CheckError(f"ops[{i}]: iterations must be 1 to 200")
    if "keep" in op and _strs(op["keep"]) is None:
        raise CheckError(f"ops[{i}]: keep must be a net or a list of nets")
    if kind == "reroute":
        r = op["region"]
        if not (isinstance(r, list) and len(r) == 4
                and all(_num(x) for x in r) and r[0] < r[2] and r[1] < r[3]):
            raise CheckError(f"ops[{i}]: region must be [x0, y0, x1, y1] "
                             "with x0 < x1, y0 < y1")
        if ("from" in op) != ("to" in op):
            raise CheckError(f"ops[{i}]: reroute takes from and to together"
                             " (or neither: join the net's islands)")
    if kind == "route_group":
        jobs = op["jobs"]
        if not (isinstance(jobs, list) and jobs):
            raise CheckError(f"ops[{i}]: jobs must be a non-empty list")
        for n, jb in enumerate(jobs):
            if not isinstance(jb, dict) or not {"net", "from", "to"} <= jb.keys():
                raise CheckError(f"ops[{i}].jobs[{n}]: needs net, from, to")
            if jb.keys() - JOB_KEYS:
                raise CheckError(f"ops[{i}].jobs[{n}]: unknown keys "
                                 f"{sorted(jb.keys() - JOB_KEYS)}")
            for k in ("net", "from", "to"):
                if not (isinstance(jb[k], str) and jb[k]):
                    raise CheckError(f"ops[{i}].jobs[{n}]: {k} must be a "
                                     "non-empty string")
            if "width" in jb and not (_num(jb["width"]) and jb["width"] > 0):
                raise CheckError(f"ops[{i}].jobs[{n}]: width must be a "
                                 "positive number")


def _strs(v) -> list[str] | None:
    """A name or a list of names -> list, None when malformed."""
    v = [v] if isinstance(v, str) else v
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return v
    return None


def _validate_selector(i: int, op: dict) -> None:
    if not op.keys() & SCOPE_KEYS:
        raise CheckError(f"ops[{i}] ({op['op']}): needs at least one of "
                         f"{sorted(SCOPE_KEYS)} (a whole-board rip is "
                         "never what an ECO means)")
    for k in ("net", "pads", "uuid_prefix"):
        if k in op and _strs(op[k]) is None:
            raise CheckError(f"ops[{i}]: {k} must be a name or a list of "
                             "names")
    if op.get("kind", "any") not in ("track", "via", "any"):
        raise CheckError(f"ops[{i}]: kind must be track|via|any")
    if op.get("touch", "end") not in ("end", "any"):
        raise CheckError(f"ops[{i}]: touch must be end|any")
    if "touch" in op and "at" not in op:
        raise CheckError(f"ops[{i}]: touch goes with at")
    if "at" in op and not _pt(op["at"]):
        raise CheckError(f"ops[{i}]: at must be [x, y] mm")
    r = op.get("region")
    if r is not None and not (isinstance(r, list) and len(r) == 4
                              and all(_num(x) for x in r)
                              and r[0] <= r[2] and r[1] <= r[3]):
        raise CheckError(f"ops[{i}]: region must be [x0, y0, x1, y1] with "
                         "x0 <= x1, y0 <= y1")
    if "radius" in op and not (_num(op["radius"]) and op["radius"] >= 0):
        raise CheckError(f"ops[{i}]: radius must be >= 0")
    if "radius" in op and not op.keys() & {"at", "pads"}:
        raise CheckError(f"ops[{i}]: radius goes with at or pads")


def select_items(bg, op: dict) -> list:
    """Tracks/vias (geom items with a uuid) an ECO selector picks, in board
    order. Pure: the venv-side twin of the hi-fi boards' rip_* scripts."""
    from shapely.geometry import Point

    nets = set(_strs(op["net"])) if "net" in op else None
    layer = op.get("layer")
    kind = "track" if layer else op.get("kind", "any")
    prefixes = tuple(_strs(op["uuid_prefix"])) if "uuid_prefix" in op \
        else None
    region = op.get("region")
    at = Point(op["at"]) if "at" in op else None
    rad = op.get("radius", AT_TOL if at is not None else 0.0)
    pads = None
    if "pads" in op:
        want = [p.split(".", 1) for p in _strs(op["pads"])]
        pads = [p for p in bg.pads_of()
                if p.net and any(p.ref == w[0]
                                 and (len(w) == 1 or p.number == w[1])
                                 for w in want)]

    def ends(it):
        if isinstance(it, geom.Via):
            return [it.at]
        c = list(it.shape.coords)
        return [c[0], c[-1]]

    items = []
    if kind in ("track", "any"):
        items += bg.tracks_of(layer=layer)
    if kind in ("via", "any"):
        items += bg.vias_of()
    out = []
    for it in items:
        if it.uuid is None or (nets is not None and it.net not in nets):
            continue
        if prefixes is not None and not it.uuid.startswith(prefixes):
            continue
        pts = ends(it)
        if region is not None and not any(
                region[0] <= x <= region[2] and region[1] <= y <= region[3]
                for x, y in pts):
            continue
        if at is not None:
            near = any(at.distance(Point(q)) <= rad + 1e-9 for q in pts)
            if not near and op.get("touch") == "any" \
                    and isinstance(it, geom.Track):
                near = it.shape.distance(at) <= rad + 1e-9
            if not near:
                continue
        if pads is not None and not any(
                p.net == it.net and p.poly.distance(Point(q)) <= rad + 1e-9
                for p in pads for q in pts):
            continue
        out.append(it)
    return out


def resolve_ops(bg, ops: list[dict], guards: dict | None = None
                ) -> tuple[list[dict], list[list[int]]]:
    """ECO ops -> worker ops. Returns (worker ops, for each input op the
    indices of the worker ops it became). rename_net passes through (the
    worker does it board-wide; there is nothing to pre-select). The routing
    ops search around what the ops before them in the list removed and
    added; reroute puts {net: "le" | "lt"} in guards: each ripped net must
    end with no more islands (DRC-free copper islands, route_finish's
    net_islands) than it had, its target with fewer."""
    flat: list[dict] = []
    spans: list[list[int]] = []
    router = None
    for op in ops:
        kind = op["op"]
        start = len(flat)
        if kind in ROUTE_OPS:
            router = _router(bg, op, router, flat)
            flat += _resolve_route(router, op, guards)
        elif kind == "rip":
            flat += [{"op": "remove", "uuid": it.uuid}
                     for it in select_items(bg, op)]
        elif kind == "renet":
            flat += [{"op": "set_net", "uuid": it.uuid, "net": op["to"]}
                     for it in select_items(bg, op) if it.net != op["to"]]
        elif kind == "set_width":
            flat += [{"op": "set_width", "uuid": it.uuid,
                      "width": op["width"]}
                     for it in select_items(bg, {**op, "kind": "track"})
                     if abs(it.width - op["width"]) > POS_TOL]
        elif kind == "fill_vias":
            sel = {k: v for k, v in op.items() if k != "filled"}
            flat += [{"op": "set_via_fill", "uuid": it.uuid,
                      "filled": op.get("filled", True)}
                     for it in select_items(bg, {**sel, "kind": "via"})]
        elif kind == "move_via":
            flat += _resolve_move_via(bg, op)
        elif kind == "join_end":
            flat += _resolve_join_end(bg, op)
        elif kind == "copy_from":
            flat += _resolve_copy_from(bg, op)
        elif kind == "set_point":
            _check_set_point(bg, op)
            flat.append(dict(op))
        elif kind == "add_path":
            pts = op["points"]
            flat += [{"op": "add_track", "start": list(a), "end": list(b),
                      "width": op["width"], "layer": op["layer"],
                      "net": op["net"]}
                     for a, b in zip(pts, pts[1:]) if not _close(a, b)]
        else:
            flat.append(dict(op))
            if kind == "add_via" and "teardrops" in op:
                # every setting spelled out: the worker sets them all, and
                # verify checks them all (#1138)
                flat[-1]["teardrops"] = dict(tdl.ITEM_PARAMS,
                                             **op["teardrops"])
        spans.append(list(range(start, len(flat))))
    return flat, spans


def _router(bg, op: dict, router, flat: list[dict]):
    """The Router for a routing op: one per list while layers and grid stay
    the same, so each route keeps clear of the last; it sees the copper the
    ops before it removed (hidden) and added."""
    import mazeroute
    import route_finish as rf

    layers = _strs(op["layers"]) if "layers" in op else None
    grid = op.get("grid", mazeroute.GRID)
    if router is None or router.g != grid or (
            layers is not None and router.layers != layers):
        router = mazeroute.Router(bg, layers=layers, grid=grid)
        router.synced = 0
    new = flat[router.synced:]
    router.obs.hidden |= {f["uuid"] for f in new if f["op"] == "remove"}
    adds = [f for f in new if f["op"] in ("add_track", "add_via")]
    if adds:
        router.commit(adds)
    router.synced = len(flat)
    return router


def _resolve_route(router, op: dict, guards: dict | None) -> list[dict]:
    kind = op["op"]
    if kind == "route":
        out = router.route(op["net"], op["from"], op["to"], op.get("width"))
    elif kind == "route_group":
        out = router.group(op["jobs"], **({"iterations": op["iterations"]}
                                          if "iterations" in op else {}))
    else:
        removed, out, _ = router.reroute(
            op["net"], op["region"], op.get("from"), op.get("to"),
            width=op.get("width"), keep=set(_strs(op.get("keep")) or ()))
        out = [{"op": "remove", "uuid": u} for u in removed] + out
        victims = {it.net for it in router.obs.items
                   if it.uuid in set(removed)}
        if guards is not None:
            for v in victims:
                guards.setdefault(v, "le")
            if "from" not in op:
                guards[op["net"]] = "lt"
    # route_edit's synthetic adds carry no uuid: the router's own
    # bookkeeping is already committed, so mark these as seen
    router.synced += len(out)
    return out


def check_guards(before_bg, after_bg, guards: dict) -> list[str]:
    """Problems with reroute's island guards, [] when every one holds."""
    import route_finish as rf

    bad = []
    for net, how in sorted(guards.items()):
        b = len(rf.net_islands(before_bg, net))
        a = len(rf.net_islands(after_bg, net))
        if a > b or (how == "lt" and a >= b):
            bad.append(f"{net}: {b} -> {a} islands")
    return bad


def _straight(t) -> bool:
    return len(t.shape.coords) == 2


def _check_set_point(bg, op: dict) -> None:
    """A public set_point names a via (which: at) or a straight track
    (start, end) on the board: an arc's end moved bends it through its old
    mid point (move_via's drag refuses arcs the same way)."""
    u, which = op["uuid"], op["which"]
    via = next((v for v in bg.vias_of() if v.uuid == u), None)
    trk = None if via else next((t for t in bg.tracks_of() if t.uuid == u),
                                None)
    if via is None and trk is None:
        raise CheckError(f"set_point: no track or via {u} on the board")
    if (via is not None) != (which == "at"):
        raise CheckError(f"set_point: which {which} on a "
                         f"{'via' if via else 'track'} {u} (at is a via's "
                         "centre, start and end a track's)")
    if trk is not None and not _straight(trk):
        raise CheckError(f"set_point: {u} is an arc - set_point moves "
                         "straight tracks only; rip the arc and add it again")


def _resolve_move_via(bg, op: dict) -> list[dict]:
    """The via at `at` goes to `to`; with drag (the default) every straight
    same-net track end on its centre goes along, so the copper stays joined
    (move_via.py removed the stub instead and left a gap to reroute)."""
    from shapely.geometry import Point

    at = Point(op["at"])
    vias = [v for v in bg.vias_of(net=op.get("net"))
            if at.distance(Point(v.at)) <= AT_TOL and v.uuid]
    if not vias:
        return []  # absent (or already moved): a re-run is a no-op
    if len(vias) > 1:
        raise CheckError(f"move_via: {len(vias)} vias at {op['at']} - "
                         "give net to pick one")
    v = vias[0]
    out = [{"op": "set_point", "uuid": v.uuid, "which": "at",
            "to": list(op["to"])}]
    if not op.get("drag", True):
        return out
    for t in bg.tracks_of(net=v.net):
        c = list(t.shape.coords)
        for which, q in (("start", c[0]), ("end", c[-1])):
            if Point(q).distance(Point(v.at)) > AT_TOL:
                continue
            if not _straight(t):
                raise CheckError(
                    f"move_via: arc {t.uuid} ends on the via - drag moves "
                    "straight tracks only; rip the arc first or drag false")
            out.append({"op": "set_point", "uuid": t.uuid, "which": which,
                        "to": list(op["to"])})
    return out


def _resolve_join_end(bg, op: dict) -> list[dict]:
    """A track's free end at `at` is joined to the nearest same-net anchor
    on its layer within `radius` (a track end, a via centre or a pad's
    copper centre) by a new track of the same width - fix_dangle.py, which
    joined the DRC's dangling ends after a footprint swap."""
    from shapely.geometry import Point

    at = Point(op["at"])
    rad = op.get("radius", JOIN_RADIUS)
    ends = []
    for t in bg.tracks_of(net=op.get("net"), layer=op.get("layer")):
        c = list(t.shape.coords)
        for q in (c[0], c[-1]):
            if Point(q).distance(at) <= AT_TOL:
                ends.append((t, q))
    if not ends:
        raise CheckError(f"join_end: no track ends at {op['at']}")
    if len({(t.net, t.layer) for t, _ in ends}) > 1:
        raise CheckError(f"join_end: track ends of several nets/layers at "
                         f"{op['at']} - give net and layer")
    if len(ends) > 1:
        return []  # two track ends meet there: a joint, not a free end
    t0, end = ends[0]
    anchors = []
    for t in bg.tracks_of(net=t0.net, layer=t0.layer):
        if t.uuid == t0.uuid:
            continue
        c = list(t.shape.coords)
        anchors += [c[0], c[-1]]
    anchors += [v.at for v in bg.vias_of(net=t0.net) if v.spans(t0.layer)]
    anchors += [p.copper_center for p in bg.pads_of(net=t0.net)
                if p.on(t0.layer)]
    pe = Point(end)
    best = min(anchors, key=lambda q: pe.distance(Point(q)), default=None)
    if best is None or pe.distance(Point(best)) > rad + 1e-9:
        raise CheckError(f"join_end: no {t0.net} anchor on {t0.layer} "
                         f"within {rad} mm of {op['at']}")
    if _close(end, best):
        return []  # already joined
    return [{"op": "add_track", "start": [end[0], end[1]],
             "end": [best[0], best[1]], "width": t0.width,
             "layer": t0.layer, "net": t0.net}]


def _resolve_copy_from(bg, op: dict) -> list[dict]:
    """The tracks and vias the selector picks on ANOTHER board (an earlier
    revision: `git show REV:board.kicad_pcb > old.kicad_pcb`) are added
    here - set5v.py, which put +5V's routing back from a saved copy. Pair
    it with a rip of the same selector to replace what is there now."""
    src = Path(op["pcb"])
    if not src.is_file():
        raise CheckError(f"copy_from: board not found: {src}")
    other = geom.BoardGeom.from_file(src)
    sel = {k: v for k, v in op.items() if k not in ("op", "pcb")}
    out = []
    for it in select_items(other, sel):
        if it.net not in bg.nets:
            raise CheckError(f"copy_from: net '{it.net}' is not on this "
                             "board")
        if isinstance(it, geom.Via):
            span = {} if tuple(it.layers) == tuple(bg.copper_layers) else {
                "kind": it.kind, "top": it.layers[0], "bot": it.layers[-1]}
            out.append({"op": "add_via", "at": list(it.at),
                        "size": it.diameter, "drill": it.drill,
                        "net": it.net, **span})
            continue
        if it.layer not in bg.copper_layers:
            raise CheckError(f"copy_from: layer {it.layer} is not on this "
                             "board")
        if not _straight(it):
            raise CheckError(f"copy_from: arc {it.uuid} - route_edit adds "
                             "straight tracks only")
        c = list(it.shape.coords)
        out.append({"op": "add_track", "start": list(c[0]),
                    "end": list(c[-1]), "width": it.width,
                    "layer": it.layer, "net": it.net})
    return out


def _close(a: tuple[float, float], b) -> bool:
    return abs(a[0] - b[0]) <= POS_TOL and abs(a[1] - b[1]) <= POS_TOL


def _verify(staged: Path, ops: list[dict]) -> list[str]:
    bg = geom.BoardGeom.from_file(staged)
    text = staged.read_text(encoding="utf-8")
    fills = _via_fills(staged) if any(op["op"] == "set_via_fill"
                                      for op in ops) else {}
    drops = _via_teardrops(text) if any(
        op["op"] == "add_via" and "teardrops" in op for op in ops) else {}
    problems = []
    for i, op in enumerate(ops):
        kind = op["op"]
        if kind == "add_track":
            want_s, want_e = tuple(op["start"]), tuple(op["end"])
            found = False
            for t in bg.tracks_of(net=op["net"], layer=op["layer"]):
                if abs(t.width - op["width"]) > POS_TOL:
                    continue
                c = list(t.shape.coords)
                s, e = c[0], c[-1]
                if (_close(want_s, s) and _close(want_e, e)) or \
                        (_close(want_s, e) and _close(want_e, s)):
                    found = True
                    break
            if not found:
                problems.append(f"ops[{i}]: track {want_s}->{want_e} "
                                f"({op['net']}, {op['layer']}) not in saved board")
        elif kind == "add_via":
            want = tuple(op["at"])
            hits = [v for v in bg.vias_of(net=op["net"])
                    if _close(want, v.at)
                    and abs(v.diameter - op["size"]) <= POS_TOL
                    and abs(v.drill - op["drill"]) <= POS_TOL
                    and v.kind == op.get("kind", "through")
                    and ("top" not in op or {op["top"], op["bot"]}
                         == {v.layers[0], v.layers[-1]})]
            if not hits:
                problems.append(f"ops[{i}]: via at {want} ({op['net']}) "
                                "not in saved board")
            elif "teardrops" in op and not any(
                    tdl.same_params(op["teardrops"], drops.get(v.uuid))
                    for v in hits):
                problems.append(f"ops[{i}]: via at {want} ({op['net']}) "
                                "saved without the teardrops asked for")
        elif kind == "remove":
            if op["uuid"] in text:
                problems.append(f"ops[{i}]: uuid {op['uuid']} still present")
        elif kind == "set_net":
            hit = [it for it in bg.tracks_of() + bg.vias_of()
                   if it.uuid == op["uuid"]]
            if not hit or hit[0].net != op["net"]:
                problems.append(
                    f"ops[{i}]: {op['uuid']} not on {op['net']} in saved "
                    "board (KiCad re-derives copper's net from the pads it "
                    "touches when it saves: renet the pads too - place_edit "
                    "set_pad - or use rename_net)")
        elif kind == "set_width":
            hit = [t for t in bg.tracks_of() if t.uuid == op["uuid"]]
            if not hit or abs(hit[0].width - op["width"]) > POS_TOL:
                problems.append(f"ops[{i}]: {op['uuid']} is not "
                                f"{op['width']} mm wide in saved board")
        elif kind == "set_point":
            hit = [it for it in bg.tracks_of() + bg.vias_of()
                   if it.uuid == op["uuid"]]
            if not hit:
                problems.append(f"ops[{i}]: {op['uuid']} not in saved board")
            else:
                it = hit[0]
                got = it.at if isinstance(it, geom.Via) else \
                    list(it.shape.coords)[0 if op["which"] == "start"
                                           else -1]
                if not _close(tuple(op["to"]), got):
                    problems.append(f"ops[{i}]: {op['uuid']} {op['which']} "
                                    f"at {got}, not {op['to']}")
        elif kind == "set_via_fill":
            got = fills.get(op["uuid"])
            if got is None:
                problems.append(f"ops[{i}]: via {op['uuid']} not in saved "
                                "board")
            elif got != (op["filled"], op["filled"]):
                problems.append(f"ops[{i}]: via {op['uuid']} filling/"
                                f"capping {got}, not {op['filled']}")
        elif kind == "rename_net":
            left = (len(bg.tracks_of(net=op["from"]))
                    + len(bg.vias_of(net=op["from"]))
                    + len(bg.pads_of(net=op["from"]))
                    + len(bg.zones_of(net=op["from"])))
            if left:
                problems.append(f"ops[{i}]: {left} items still on "
                                f"{op['from']}")
    return problems


def _via_teardrops(text: str) -> dict:
    """uuid -> teardroplib.params_of per via of the saved board."""
    import sexpdata
    return tdl.via_params(sexpdata.loads(text))


def _via_fills(pcb: Path) -> dict:
    """uuid -> (filled, capped) per via, board defaults applied - the
    reading check_wicking and fab_export.via_fill_facts use."""
    import sexpdata
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import check_wicking as cw
    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    dflt = cw.board_defaults(root)
    return {v["uuid"]: (bool(v["filled"]), bool(v["capped"]))
            for v in cw.read_vias(root, dflt, ["F.Cu", "B.Cu"])}


def apply_ops(pcb: Path, ops: list[dict], *, refill: bool = False) -> list:
    """Validate, resolve ECO selectors, stage, run the SWIG worker, verify,
    atomically swap in.

    Returns one result per input op (a selector op's result counts and names
    what it resolved to). Raises CheckError (original board untouched) on any
    failure. Importable - generators use this directly.
    """
    pcb = Path(pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    bg = geom.load_board(pcb, refresh=True)
    known_nets = set(bg.nets) | {op["to"] for op in ops
                                 if op["op"] == "rename_net"}
    copper = set(bg.copper_layers)
    for i, op in enumerate(ops):
        want = []
        if op["op"] in SELECTOR_OPS:
            want = _strs(op["net"]) if "net" in op else []
        elif "net" in op:
            want = [op["net"]]
        if op["op"] == "renet":
            want.append(op["to"])
        if op["op"] == "rename_net":
            want.append(op["from"])
        for n in want:
            if n not in known_nets:
                raise CheckError(f"ops[{i}]: net '{n}' not on board")
        for jb in op.get("jobs", []) if op["op"] == "route_group" else []:
            if jb["net"] not in known_nets:
                raise CheckError(f"ops[{i}]: net '{jb['net']}' not on board")
        for ly in _strs(op.get("layers")) or [] \
                if op["op"] in ROUTE_OPS else []:
            if ly not in copper:
                raise CheckError(f"ops[{i}]: layer '{ly}' is not a copper "
                                 "layer of this board")
        if "layer" in op and op["layer"] not in copper:
            raise CheckError(f"ops[{i}]: layer '{op['layer']}' is not a "
                             f"copper layer of this board")
        if "pads" in op and op["op"] != "copy_from":
            refs = {p.ref for p in bg.pads_of()}
            gone = [x for x in _strs(op["pads"])
                    if x.split(".", 1)[0] not in refs]
            if gone:
                raise CheckError(f"ops[{i}]: pads of parts not on board: "
                                 f"{gone}")
    guards: dict = {}
    flat, spans = resolve_ops(bg, ops, guards)

    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")

    stage = Path(tempfile.mkdtemp(prefix=".aiee_route_", dir=pcb.parent))
    try:
        staged = stage / pcb.name
        shutil.copy2(pcb, staged)
        worker = routelib.run_worker(
            bp, {"verb": "apply_ops", "board": str(staged),
                 "out": str(staged), "ops": flat}, stage)["results"] \
            if flat else []
        problems = _verify(staged, flat)
        if guards and not problems:
            problems = [f"reroute guard: {p}" for p in check_guards(
                bg, geom.BoardGeom.from_file(staged), guards)]
        if problems:
            raise CheckError("post-apply verify failed (rolled back): "
                             + "; ".join(problems[:10]))
        if refill and flat:
            _refill(cli, pcb, staged)
        if flat:
            os.replace(staged, pcb)
        return [_fold(op, [worker[j] for j in span], [flat[j] for j in span])
                for op, span in zip(ops, spans)]
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _fold(op: dict, results: list[dict], flat: list[dict]) -> dict:
    """One input op's result from the worker results it expanded into."""
    if op["op"] in OP_FIELDS or op["op"] == "rename_net":
        return results[0]
    out = {"op": op["op"], "count": len(results),
           "status": "absent" if not results else
           {"rip": "removed", "renet": "renetted", "add_path": "added",
            "set_width": "changed", "move_via": "moved",
            "join_end": "added", "copy_from": "added",
            "route": "added", "route_group": "added", "reroute": "rerouted",
            "fill_vias": "filled" if op.get("filled", True)
            else "unfilled"}[op["op"]]}
    if op["op"] in ("rip", "renet", "set_width", "move_via", "fill_vias"):
        out["uuids"] = [f["uuid"] for f in flat]
    if op["op"] == "fill_vias":
        out["segments"] = [r["status"] for r in results]
        if results and all(r["status"] == "exists" for r in results):
            out["status"] = "exists"
    elif op["op"] not in ("rip", "renet", "set_width", "move_via"):
        out["segments"] = [r["status"] for r in results]
    return out


def _refill(cli, pcb: Path, staged: Path) -> None:
    """Refill zones on the staged copy, with the board's own project files
    beside it (kicad-cli would refill against default rules otherwise)."""
    import kc

    for ext in (".kicad_pro", ".kicad_dru"):
        side = pcb.with_suffix(ext)
        if side.is_file():
            shutil.copy2(side, staged.with_suffix(ext))
    try:
        kc.run_drc(cli, staged, refill=True, save_board=True)
    except Exception as e:  # noqa: BLE001 - any refill failure rolls back
        raise CheckError(f"zone refill failed (rolled back): {e}") from e


def load_ops(ops_file: str | None, inline: list[str]) -> list[dict]:
    """--ops FILE and/or --op JSON (repeatable) -> validated op list."""
    ops: list = []
    if ops_file:
        doc = checklib.load_json(ops_file, "ops file")
        if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
            raise CheckError("ops file must be {'version': 1, 'ops': [...]}")
        ops += doc["ops"]
        if doc.get("version") != 1:
            raise CheckError("ops file must be {'version': 1, 'ops': [...]}")
    for raw in inline:
        try:
            op = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CheckError(f"--op is not JSON: {e}") from e
        ops += op if isinstance(op, list) else [op]
    if not ops_file and not inline:
        raise CheckError("give --ops FILE or one or more --op JSON")
    return validate_ops({"version": 1, "ops": ops})


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--ops", default=None, help="ops file {version: 1, ops}")
    ap.add_argument("--op", action="append", default=[],
                    help="one op (or a list) as JSON; repeatable")
    ap.add_argument("--refill", action="store_true",
                    help="refill zones on the staged copy before the swap")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    ops = load_ops(args.ops, args.op)
    results = apply_ops(Path(args.pcb), ops, refill=args.refill)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    payload = {"script": "route_edit", "status": "pass", "board": args.pcb,
               "applied": len(ops), "verified": True, "by_status": counts,
               "refilled": args.refill, "results": results}
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("route_edit", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
