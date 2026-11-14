"""part_repair - move, rotate or re-pad parts on a routed board and mend the copper round them.

  hwde part_repair --pcb B.kicad_pcb (--ops FILE | --op JSON ...) --out DIR
                   [--margin 2] [--keep NET ...] [--apply] [--out-report R]

The ops are place_edit's footprint ops (place, move, rotate, flip, swap_fp,
renumber). On a routed board each leaves copper on the wrong pads: a moved
part's tracks end in the air, and a re-padded part's tracks touch pads of
another net, which KiCad would carry onto that net when it saves. This tool
does what was done by hand round a part move (the haos-voice C216-C224 moves,
the Y301 re-pad): rip the copper the move spoils, make the move, route every
net back together, and keep the result only if the board got no worse.

  1. base: --pcb is copied to DIR/base, refilled and DRC'd. That DRC and each
     net's island count (route_finish.net_islands) are the yardstick.
  2. trial: the ops run on a scratch copy (DIR/trial) to learn where every
     pad lands and on which net.
  3. window: per part, the box round its old and new pads grown by --margin.
  4. rip: tracks and vias that touch an old pad and no new pad of their net,
     and other-net copper the new pads would short or come too close to
     (board_update.swap_clashes). Behind each goes the chain of track up to
     the first via, pad, junction, same-net pour or free end, or up to the
     first segment that leaves the window: that one stays.
  5. work: DIR/work gets the rips (route_edit), then the ops (place_edit,
     refilled, with its touch check on: no other-net copper may touch a new
     pad, so a rip that missed something fails here).
  6. join: every net the rips or the parts touch is routed back to as few
     islands as it had (lib/mazeroute.py), the parts' nets first: at the
     widest width ripped from it, then at its rule width, then letting the
     maze router push other signal nets' copper in the window aside and put
     it back (Router.reroute). Dead stubs left in the windows are pruned
     (route_cleanup's dangling pass): none the new routes meet, except a
     spur off a junction they make with copper that holds without it.
  7. check: the routes go in, the board is refilled and DRC'd. It passes when
     no net has more islands than before, no DRC check counts more errors or
     warnings than before, and no net has more unconnected items.

In DIR on a pass: 01-rip.route_edit.json, 02-place.place_edit.json and
03-route.route_edit.json (prunes and pushes removed, then the new routes),
which replay on --pcb in this order:

  hwde route_edit --pcb B --ops DIR/01-rip.route_edit.json
  hwde place_edit --pcb B --ops DIR/02-place.place_edit.json --allow-routed
  hwde route_edit --pcb B --ops DIR/03-route.route_edit.json --refill

An op file with nothing in it is not written (and its step is skipped).
--apply does all three at once: DIR/work/B replaces --pcb, on a pass only and
only if --pcb has not changed since the run began. Always in DIR: REPORT.md,
part_repair.json, and base/, trial/ and work/ with their boards. Zones are
refilled, not reshaped: a PATCH_<net> zone (zone_patch) whose fill met an
old pad and meets no new pad of its net is named in the report's notes.

Contract: --pcb is read only, except with --apply on a pass. Exit 0 pass,
1 violations (a net would not join, or the board got worse: --pcb untouched,
no op files), 2 error.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import shapely  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402

import board_update  # noqa: E402
import checklib  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import mazeroute  # noqa: E402
import place_edit  # noqa: E402
import route_auto  # noqa: E402
import route_cleanup  # noqa: E402
import route_edit  # noqa: E402
import route_finish as rf  # noqa: E402
import route_improve  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

MARGIN = 2.0   # mm round a part's old and new pads: the rip window
TOL = 1e-3     # mm
RIP_FILE = "01-rip.route_edit.json"
PLACE_FILE = "02-place.place_edit.json"
ROUTE_FILE = "03-route.route_edit.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _ends(t) -> tuple:
    c = list(t.shape.coords)
    return tuple(c[0]), tuple(c[-1])


def _touches(it, pad) -> bool:
    if isinstance(it, geom.Track):
        return it.layer in pad.layers and it.poly.intersects(pad.poly)
    return any(it.spans(ly) for ly in pad.layers) \
        and it.poly.intersects(pad.poly)


def _kind(it) -> str:
    return "track" if isinstance(it, geom.Track) else "via"


def _r(v) -> float:
    return round(float(v), 4)


def windows(old: dict, new: dict, margin: float) -> dict:
    """ref -> the box round its old and new pads, grown by margin."""
    out = {}
    for ref in old:
        polys = [p.poly for p in old[ref] + new.get(ref, [])]
        if polys:
            x0, y0, x1, y1 = shapely.union_all(polys).bounds
            out[ref] = box(x0 - margin, y0 - margin, x1 + margin,
                           y1 + margin)
    return out


def seeds(bg, old: dict, new: dict) -> list:
    """Tracks and vias that touch an old pad and no new pad of their net."""
    out: dict = {}
    for ref, pads in old.items():
        for p in pads:
            for it in board_update._touching(bg, p):
                if not any(board_update._nets_equal(it.net, q.net)
                           and _touches(it, q) for q in new.get(ref, [])):
                    out[it.uuid] = it
    return list(out.values())


class Rip:
    """The copper to rip on the base board, chain by chain."""

    def __init__(self, bg, win, keep):
        self.bg, self.win, self.keep = bg, win, set(keep)
        self.tracks: dict = {}
        for t in bg.tracks_of():
            self.tracks.setdefault((t.net, t.layer), []).append(t)
        self.vias: dict = {}
        for v in bg.vias_of():
            self.vias.setdefault(v.net, []).append(v)
        self.pads: dict = {}
        for p in bg.pads_of():
            self.pads.setdefault(p.net, []).append(p)
        self._fills: dict = {}
        self.items: dict = {}       # uuid -> track or via, in rip order

    def _fill(self, net, layer):
        if (net, layer) not in self._fills:
            self._fills[(net, layer)] = self.bg.zone_fill(net, layer)
        return self._fills[(net, layer)]

    def _take(self, it, why: str) -> bool:
        if not it.uuid:
            raise CheckError(f"{why}: a {_kind(it)} of {it.net!r} has no"
                             " uuid, so no op can remove it")
        if it.net in self.keep:
            raise CheckError(f"{why}: {it.net} is --keep, but its {_kind(it)}"
                             f" {it.uuid[:8]} must go")
        if it.uuid in self.items:
            return False
        self.items[it.uuid] = it
        return True

    def next_track(self, cur, p):
        """The one track the chain from cur goes on to at its end p, or None
        where it stops: a via, a pad, the net's pour, a free end, a
        junction, a T onto a track's middle, an arc, or a segment that
        leaves the window (it stays)."""
        net, layer, half = cur.net, cur.layer, cur.width / 2
        pt = Point(p)
        for v in self.vias.get(net, []):
            if v.spans(layer) and math.dist(p, v.at) \
                    <= v.diameter / 2 + half + TOL:
                return None
        for q in self.pads.get(net, []):
            if layer in q.layers and q.poly.distance(pt) <= half + TOL:
                return None
        fill = self._fill(net, layer)
        if fill is not None and not fill.is_empty \
                and fill.distance(pt) <= half + TOL:
            return None
        near = [t for t in self.tracks.get((net, layer), [])
                if t.uuid != cur.uuid
                and t.shape.distance(pt) <= (cur.width + t.width) / 2 + TOL]
        if len(near) != 1:
            return None
        t = near[0]
        if t.uuid in self.items or len(t.shape.coords) != 2:
            return None
        a, b = _ends(t)
        if min(math.dist(a, p), math.dist(b, p)) \
                > max(TOL, min(cur.width, t.width) / 2):
            return None
        if not self.win.contains(t.poly):
            return None
        return t

    def walk(self, t, why: str) -> None:
        """Rip track t and the chain behind both its ends."""
        if not self._take(t, why):
            return
        for p in _ends(t):
            cur = t
            while True:
                nxt = self.next_track(cur, p)
                if nxt is None or not self._take(nxt, why):
                    break
                a, b = _ends(nxt)
                p = b if math.dist(a, p) <= math.dist(b, p) else a
                cur = nxt

    def via(self, v, why: str) -> None:
        """Rip via v and, behind it, its tracks that lie in the window."""
        if not self._take(v, why):
            return
        for ly in self.bg.copper_layers:
            if not v.spans(ly):
                continue
            for t in self.tracks.get((v.net, ly), []):
                if t.uuid not in self.items and t.poly.intersects(v.poly) \
                        and self.win.contains(t.poly):
                    self.walk(t, why)

    def add(self, it, why: str) -> None:
        if isinstance(it, geom.Track):
            self.walk(it, why)
        else:
            self.via(it, why)


def _restore(router, held) -> None:
    router.obs.hidden = set(held[0])
    router.obs.added = list(held[1])
    router._conn = held[2]


def _held(router):
    return set(router.obs.hidden), list(router.obs.added), router._conn


def join_net(router, net: str, want: int, near, width, keep):
    """Route net back to `want` islands: (ops, uuids pushed aside, how,
    {net pushed: islands it may end with}). Raises CheckError with every
    try's reason when none works; the router is then as it was."""
    why = []
    for w in dict.fromkeys([width, None]):
        held = _held(router)
        try:
            ops = router.join_islands(net, want, near, w)
            how = "rule width" if w is None else f"width {w:g}"
            return ops, [], how, {}
        except CheckError as e:
            _restore(router, held)
            why.append(f"{'rule width' if w is None else f'width {w:g}'}:"
                       f" {e}")
    held = _held(router)
    try:
        uuids, ops, maxes = router.reroute(net, near.bounds, keep=keep)
        ops += router.join_islands(net, want, near)
        return ops, uuids, "pushed other nets aside", maxes
    except CheckError as e:
        _restore(router, held)
        why.append(f"push: {e}")
    raise CheckError(f"{net}: " + "; ".join(why))


def _via_layers(op: dict, cu: list) -> list:
    if "top" in op and "bot" in op and op["top"] in cu and op["bot"] in cu:
        i, j = sorted((cu.index(op["top"]), cu.index(op["bot"])))
        return cu[i:j + 1]
    return list(cu)


def _spur_end(s, poly, added, segs, vias, pads, arcs, fill):
    """For an existing track s (copper `poly`) that meets the `added`
    copper of its net and layer: the uuids of the existing tracks and vias
    at its end E (a set, maybe empty) when s may still be pruned, else
    None. It may when every added piece that meets s lies at E and none at
    its other end, and the copper at E without s (the added pieces and the
    existing tracks, arcs, vias, pads and pour that reach E) has some that
    was there before and holds together by itself, so pruning s cannot cut
    the added copper off: s is a spur the join left behind a junction."""
    tol = route_cleanup.TOL
    half = s.width / 2
    reach = half + tol
    pa, pb = Point(s.a), Point(s.b)
    ends = set()
    for p in added:
        if not p.intersects(poly):
            continue
        at_a, at_b = p.distance(pa) <= reach, p.distance(pb) <= reach
        if at_a == at_b:
            return None
        ends.add(at_a)
    if len(ends) != 1:
        return None
    e = s.a if ends.pop() else s.b
    pe = Point(e)
    have = [p for p in added if p.distance(pe) <= reach]
    old: list = []
    partners = set()
    for o in segs:
        if o is not s and o.net == s.net and o.layer == s.layer \
                and route_cleanup._pt_seg_dist(e, o.a, o.b) \
                <= half + o.width / 2 + tol:
            old.append(LineString([o.a, o.b]).buffer(o.width / 2))
            partners.add(o.uuid)
    for net, layer, line, w in arcs:
        if net == s.net and layer == s.layer \
                and line.distance(pe) <= half + w / 2 + tol:
            old.append(line.buffer(w / 2))
    for v in vias:
        if v.net == s.net and s.layer in v.layers \
                and math.dist(e, v.at) <= v.size / 2 + reach:
            old.append(Point(v.at).buffer(v.size / 2))
            partners.add(v.uuid)
    for pd in pads:
        if pd.net == s.net and s.layer in pd.layers \
                and pd.poly.distance(pe) <= reach:
            old.append(pd.poly)
    if fill is not None and not fill.is_empty and fill.distance(pe) <= reach:
        old.append(fill)
    if not old:
        return None
    polys = have + old
    group = list(range(len(polys)))

    def root(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if polys[i].intersects(polys[j]):
                group[root(i)] = root(j)
    if len({root(i) for i in range(len(polys))}) != 1:
        return None
    return partners


def _dangling(pcb: Path, bg, nets: set, *, gone=(), adds=(), win=None,
              anchors=()) -> list:
    """route_cleanup's dangling pass over `nets` on pcb, less `gone`, plus
    the `adds` (route_edit ops, never removed); `anchors` never go. With
    win, only copper fully inside it may go, and only if it meets no added
    copper or is a spur behind a junction of it (_spur_end). If such a
    spur goes and so does an existing track or via at its junction, the
    junction is checked again without what went; if it no longer holds,
    the spur is kept and the pass runs again (each run keeps more, so it
    ends)."""
    cu = list(bg.copper_layers)
    segs, vias = route_cleanup.parse_items(pcb, cu)
    gone = set(gone)
    segs = [s for s in segs if s.net in nets and s.uuid not in gone]
    vias = [v for v in vias if v.net in nets and v.uuid not in gone]
    addcu: dict = {}
    new_s, new_v = [], []
    for op in adds:
        if op["op"] == "add_track":
            addcu.setdefault((op["net"], op["layer"]), []).append(
                LineString([op["start"], op["end"]]).buffer(op["width"] / 2))
            new_s.append(route_cleanup.Seg(
                "", op["net"], op["layer"], float(op["width"]),
                tuple(op["start"]), tuple(op["end"])))
        elif op["op"] == "add_via":
            for ly in _via_layers(op, cu):
                addcu.setdefault((op["net"], ly), []).append(
                    Point(op["at"]).buffer(op["size"] / 2))
            new_v.append(route_cleanup.ViaItem(
                "", op["net"], tuple(op["at"]), float(op["size"]),
                float(op["drill"]), tuple(_via_layers(op, cu))))
    pads = [route_cleanup.PadItem(p.net, tuple(p.layers), p.center, p.poly)
            for p in bg.pads_of() if p.net in nets]
    arcs = tuple((t.net, t.layer, t.shape, t.width) for t in bg.tracks_of()
                 if t.net in nets and t.uuid not in gone
                 and len(t.shape.coords) > 2)
    cache: dict = {}

    def fillfn(net, layer):
        if (net, layer) not in cache:
            cache[(net, layer)] = bg.zone_fill(net, layer)
        return cache[(net, layer)]

    hold = set(anchors)
    spurs: dict = {}    # uuid -> (track, copper, added copper, partners)
    if win is not None:
        for s in segs:
            if s.uuid in hold:
                continue
            poly = LineString([s.a, s.b]).buffer(s.width / 2)
            if not win.contains(poly):
                hold.add(s.uuid)
                continue
            added = addcu.get((s.net, s.layer), [])
            if not any(p.intersects(poly) for p in added):
                continue
            got = _spur_end(s, poly, added, segs, vias, pads, arcs,
                            fillfn(s.net, s.layer))
            if got is None:
                hold.add(s.uuid)
            else:
                spurs[s.uuid] = (s, poly, added, got)
        for v in vias:
            poly = Point(v.at).buffer(v.size / 2)
            if not win.contains(poly) or any(
                    p.intersects(poly) for ly in v.layers
                    for p in addcu.get((v.net, ly), [])):
                hold.add(v.uuid)
    while True:
        gs, gv = route_cleanup.find_dangling(
            [dataclasses.replace(s, uuid="") if s.uuid in hold else s
             for s in segs] + new_s,
            [dataclasses.replace(v, uuid="") if v.uuid in hold else v
             for v in vias] + new_v, pads, fillfn, arcs)
        out = set(gs + gv)
        left_s = [s for s in segs if s.uuid not in out]
        left_v = [v for v in vias if v.uuid not in out]
        bad = {u for u in gs if u in spurs and spurs[u][3] & out
               and _spur_end(*spurs[u][:3], left_s, left_v, pads, arcs,
                             fillfn(spurs[u][0].net, spurs[u][0].layer))
               is None}
        if not bad:
            return gs + gv
        hold |= bad


def patch_notes(bg, old: dict, new: dict) -> list[str]:
    """PATCH_<net> zones whose fill met an old pad of their net and meets
    no new one: zone_patch's to reshape."""
    notes = []
    for z in bg.zones_of():
        if not z.name.startswith("PATCH_"):
            continue
        for ly in z.fills:
            fill = z.fill_on(ly)
            if fill.is_empty:
                continue
            for ref in old:
                met = [p for p in old[ref] if p.net == z.net
                       and ly in p.layers and fill.intersects(p.poly)]
                meets = [p for p in new.get(ref, []) if p.net == z.net
                         and ly in p.layers and fill.intersects(p.poly)]
                if met and not meets:
                    notes.append(
                        f"zone {z.name} on {ly} met {ref}."
                        f"{met[0].number} and meets no new pad of {z.net}:"
                        f" reshape it with zone_patch --net {z.net} --layer"
                        f" {ly} (or --remove)")
    return notes


def gates(drc0: dict, drc1: dict, isl: dict) -> list[str]:
    """Why the repaired board is worse than the base one (empty: it isn't)."""
    worse = [f"{n or '(no net)'}: {a} -> {b} islands"
             for n, (a, b) in sorted(isl.items()) if b > a]
    i0, i1 = route_improve.issue_counts(drc0), route_improve.issue_counts(drc1)
    worse += [f"DRC {k}: {i0.get(k, 0)} -> {v}"
              for k, v in sorted(i1.items()) if v > i0.get(k, 0)]
    u0, u1 = rf.unconnected_by_net(drc0), rf.unconnected_by_net(drc1)
    worse += [f"unconnected {n or '(no net)'}: {u0.get(n, 0)} -> {v}"
              for n, v in sorted(u1.items()) if v > u0.get(n, 0)]
    a, b = route_improve.unconnected(drc0), route_improve.unconnected(drc1)
    if b > a:
        worse.append(f"unconnected: {a} -> {b}")
    return worse


def _write_ops(path: Path, ops: list) -> None:
    path.write_text(json.dumps({"version": 1, "ops": ops}, indent=1) + "\n",
                    encoding="utf-8")


def repair(pcb: Path, ops: list, out: Path, *, margin: float = MARGIN,
           keep=(), apply: bool = False) -> dict:
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        raise CheckError("KiCad bundled python not found (env.py)")
    sha0 = _sha(pcb)
    out = routelib.fresh_work_dir(out)
    refs = list(dict.fromkeys(op["ref"] for op in ops))
    payload: dict = {"script": "part_repair", "status": "pass",
                     "board": str(pcb), "out": str(out), "refs": refs,
                     "ops": len(ops), "notes": [], "applied": False}

    # 1. base: refilled, DRC'd
    base = route_auto._stage_board(pcb, out / "base")
    drc0 = kc.run_drc(cli, base, refill=True, save_board=True)
    bg0 = geom.BoardGeom.from_file(base)

    # 2. trial: where every pad lands
    trial = route_auto._stage_board(base, out / "trial")
    place_edit.apply_ops(trial, copy.deepcopy(ops), touch_check=False)
    bgt = geom.BoardGeom.from_file(trial)
    old = {r: bg0.pads_of(ref=r) for r in refs}
    new = {r: bgt.pads_of(ref=r) for r in refs}

    # 3. windows, 4. rips
    wins = windows(old, new, margin)
    win = shapely.union_all(list(wins.values()))
    payload["windows"] = {r: [_r(v) for v in w.bounds]
                          for r, w in wins.items()}
    rip = Rip(bg0, win, keep)
    for it in seeds(bg0, old, new):
        rip.add(it, "copper on an old pad")
    clashes = 0
    for r in refs:
        hits = board_update.swap_clashes(bg0, r, new[r], base)
        clashes += len(hits)
        for it in hits:
            rip.add(it, f"other-net copper too close to {r}'s new pads")
    ripped = list(rip.items.values())
    by_net: dict = {}
    for it in ripped:
        by_net.setdefault(it.net, {"tracks": 0, "vias": 0})[
            _kind(it) + "s"] += 1
    payload["ripped"] = {
        "tracks": sum(isinstance(i, geom.Track) for i in ripped),
        "vias": sum(not isinstance(i, geom.Track) for i in ripped),
        "clashes": clashes, "by_net": by_net}
    payload["notes"] += patch_notes(bg0, old, new)

    # 5. work: rips, then the ops (refilled, touch check on)
    work = route_auto._stage_board(base, out / "work")
    rip_ops = [{"op": "remove", "uuid": it.uuid} for it in ripped]
    if rip_ops:
        route_edit.apply_ops(work, rip_ops)
    results = place_edit.apply_ops(work, copy.deepcopy(ops), refill=True,
                                   touch_check=True)
    payload["notes"] += [n for res in results if isinstance(res, dict)
                         for n in res.get("notes", [])]

    # 6. joins: the parts' nets first, then what the rips touched
    part_nets = [p.net for r in refs for p in old[r] + new[r] if p.net]
    nets = list(dict.fromkeys(part_nets + [it.net for it in ripped
                                           if it.net]))
    bgw = geom.BoardGeom.from_file(work)
    router = mazeroute.Router(bgw)
    base_isl: dict = {}

    def base_count(n):
        if n not in base_isl:
            base_isl[n] = len(rf.net_islands(bg0, n))
        return base_isl[n]

    widest: dict = {}
    for it in ripped:
        if isinstance(it, geom.Track):
            widest[it.net] = max(widest.get(it.net, 0.0), it.width)
    adds: list = []
    pushed: list = []
    joins: dict = {}
    failed: list = []
    near_of = {}
    for r, w in wins.items():
        for p in old[r] + new[r]:
            near_of[p.net] = near_of[p.net].union(w) if p.net in near_of \
                else w
    queue = list(nets)
    for net in queue:
        want, have = base_count(net), len(router.islands(net))
        joins[net] = {"islands_before": want, "after_rip": have}
        if have <= want:
            joins[net]["how"] = "still joined"
            continue
        near = near_of.get(net) or win
        try:
            jops, gone, how, maxes = join_net(
                router, net, want, near, widest.get(net), keep)
        except CheckError as e:
            joins[net]["how"] = "failed"
            failed.append(str(e))
            continue
        joins[net].update(how=how, tracks=sum(
            o["op"] == "add_track" for o in jops), vias=sum(
            o["op"] == "add_via" for o in jops))
        adds += jops
        pushed += gone
        for v in maxes:
            if v not in queue:
                queue.append(v)
                base_count(v)
    payload["joins"] = joins
    payload["pushed"] = pushed
    if failed:
        payload["status"] = "violations"
        payload["failed"] = failed
        return _finish(payload, out)

    # prune what the rips and joins left hanging in the windows
    nets = set(queue)
    anchors = set(_dangling(base, bg0, nets))
    pruned = _dangling(work, bgw, nets, gone=pushed, adds=adds, win=win,
                       anchors=anchors)
    payload["pruned"] = pruned
    route_ops = [{"op": "remove", "uuid": u}
                 for u in dict.fromkeys(pushed + pruned)] + adds
    payload["added"] = {
        "tracks": sum(o["op"] == "add_track" for o in adds),
        "vias": sum(o["op"] == "add_via" for o in adds)}

    # 7. check
    if route_ops:
        route_edit.apply_ops(work, route_ops)
    drc1 = kc.run_drc(cli, work, refill=True, save_board=True)
    bg1 = geom.BoardGeom.from_file(work)
    isl = {n: [base_count(n), len(rf.net_islands(bg1, n))] for n in nets}
    payload["islands"] = isl
    payload["drc"] = {"before": drc0["counts"], "after": drc1["counts"]}
    worse = gates(drc0, drc1, isl)
    if worse:
        payload["status"] = "violations"
        payload["worse"] = worse
        return _finish(payload, out)

    files = []
    for name, body in ((RIP_FILE, rip_ops), (PLACE_FILE, ops),
                       (ROUTE_FILE, route_ops)):
        if body:
            _write_ops(out / name, body)
            files.append(name)
    payload["op_files"] = files
    payload["replay"] = _replay(pcb, out, files)
    if apply:
        if _sha(pcb) != sha0:
            raise CheckError(f"{pcb} changed while part_repair ran: not"
                             f" applied (the op files in {out} still"
                             " replay)")
        tmp = pcb.with_name(f".{pcb.name}.part_repair")
        shutil.copy2(work, tmp)  # work is DIR/work/B itself
        os.replace(tmp, pcb)
        payload["applied"] = True
    return _finish(payload, out)


def _replay(pcb: Path, out: Path, files: list) -> list[str]:
    tail = {RIP_FILE: "route_edit", PLACE_FILE: "place_edit",
            ROUTE_FILE: "route_edit"}
    extra = {PLACE_FILE: " --allow-routed", ROUTE_FILE: " --refill"}
    return [f"hwde {tail[f]} --pcb {pcb} --ops {out / f}{extra.get(f, '')}"
            for f in files]


def _finish(payload: dict, out: Path) -> dict:
    (out / "part_repair.json").write_text(json.dumps(payload, indent=1),
                                          encoding="utf-8")
    (out / "REPORT.md").write_text(report_md(payload), encoding="utf-8")
    return payload


def report_md(p: dict) -> str:
    lines = [f"# part_repair: {p['status']}", "",
             f"Board: `{p['board']}`; parts: {', '.join(p['refs'])};"
             f" ops: {p['ops']}.", ""]
    rp = p.get("ripped")
    if rp:
        lines += [f"Ripped {rp['tracks']} tracks and {rp['vias']} vias"
                  f" ({rp['clashes']} too close to new pads).", ""]
    if p.get("joins"):
        lines += ["| net | islands before | after rip | how | tracks | vias |",
                  "|---|---|---|---|---|---|"]
        for n, j in p["joins"].items():
            lines.append(f"| {n} | {j['islands_before']} | {j['after_rip']}"
                         f" | {j.get('how', '')} | {j.get('tracks', '')}"
                         f" | {j.get('vias', '')} |")
        lines.append("")
    for key, head in (("failed", "Would not join"),
                      ("worse", "Worse than before")):
        if p.get(key):
            lines += [f"{head}:", ""] + [f"- {x}" for x in p[key]] + [""]
    if p.get("drc"):
        b, a = p["drc"]["before"], p["drc"]["after"]
        lines += [f"DRC total {b.get('total')} -> {a.get('total')}.", ""]
    if p.get("replay"):
        lines += ["Replay on the board, in order:", "", "```"] \
            + p["replay"] + ["```", ""]
    if p.get("notes"):
        lines += ["Notes:", ""] + [f"- {x}" for x in p["notes"]] + [""]
    lines.append(f"Applied to the board: {'yes' if p['applied'] else 'no'}.")
    return "\n".join(lines) + "\n"


def load_ops(ops_file: str | None, inline: list[str]) -> list[dict]:
    """place_edit's footprint ops, validated; swap_fp paths made absolute."""
    ops = place_edit.load_ops(ops_file, inline)
    bad = sorted({op["op"] for op in ops
                  if op["op"] not in place_edit.FOOTPRINT_OPS})
    if bad:
        raise CheckError(f"part_repair takes footprint ops only"
                         f" ({', '.join(sorted(place_edit.FOOTPRINT_OPS))}),"
                         f" not {', '.join(bad)}: run place_edit for those")
    for op in ops:
        if op["op"] == "swap_fp":
            op["footprint"] = str(Path(op["footprint"]).expanduser()
                                  .resolve())
    return ops


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--ops", default=None, help="ops file {version: 1, ops}")
    ap.add_argument("--op", action="append", default=[],
                    help="one op (or a list) as JSON; repeatable")
    ap.add_argument("--out", required=True,
                    help="work dir: boards, op files and the report")
    ap.add_argument("--margin", type=float, default=MARGIN,
                    help="mm round each part's old and new pads that may be"
                         f" ripped (default {MARGIN:g})")
    ap.add_argument("--keep", action="append", default=[],
                    help="a net whose copper must not be ripped; repeatable")
    ap.add_argument("--apply", action="store_true",
                    help="on a pass, put the repaired board in place of"
                         " --pcb")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)
    if args.margin <= 0:
        raise CheckError("--margin must be more than 0")
    ops = load_ops(args.ops, args.op)
    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    out = Path(args.out).resolve()
    if out == pcb.parent or out in pcb.parents:
        raise CheckError("--out must be a directory of its own, not the"
                         " board's or one above it")
    payload = repair(pcb, ops, out, margin=args.margin, keep=args.keep,
                     apply=args.apply)
    return checklib.stamp(payload, pcb), args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("part_repair", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
