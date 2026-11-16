"""check_tombstone.py - tombstone-prone small 2-pad chips (DFA-11; dfm
family `tombstone`, hive-pcb-flow #328).

The mode: an 0603-or-smaller chip whose two ends reach reflow temperature at
different times stands up on the end that wets first. An 0402's one-end
surface-tension moment is ~17x its weight moment (stencil_rules.py), so
any wetting-time skew can lift it (Indium; IPC S27-1; FCT, SMTAI 2021). The
skew comes from the copper each end is tied to: a pad poured solid into a
plane soaks up heat its partner on a thin trace doesn't. IPC-7351's cure is
symmetric copper - a thermal relief on the pour side, or matched traces.

What is measured, per end of each small 2-pad SMD chip (both pads SMD, on
one copper layer, centres <= MAX_PITCH_MM apart and no pad side over
MAX_PAD_MM - 0603 and smaller, SOD-523 and the like; a DNP part is left
out): the copper of the pad's own net that leaves the pad - the area of
same-net copper (track, zone fill, via, another pad) in a BAND_MM ring
round the pad, over BAND_MM, so a track counts its width, a thermal
relief its spokes, a solid pour the whole edge it touches - plus pi x drill
for each via inside the pad whose barrel reaches a fill of the pad's net on
another layer (a via in the pad that reaches no plane sinks little and is
not counted). The two ends are unequal when the heavier is at least RATIO
times the lighter and at least DIFF_MM more.

  dfm_tombstone   warning: the heavy end's excess is a plane - a zone fill
                  joining the pad solid (its pad or zone connection is not
                  thermal; a small island counts when a via in it runs
                  down to a plane), or a via in the pad down to one - and
                  either that plane alone makes the ends unequal or
                  without it they would not be. ONE finding per zone (and
                  layer) listing every part it heavies, its items one per
                  part; one per part for a via in the pad. Fix: thermal
                  relief on that pad (zone connect thermal), or move the
                  via out of the pad / match the other end.
  dfm_tombstone_traces  info: the ends are unequal by track width and not
                  by any plane alone (a wide supply track against a thin
                  signal one, or a pour's thermal-relief spokes: the relief
                  is the cure, so its spokes count as track, as do a
                  teardrop and a fill island under MIN_PLANE_MM2 with no
                  via down to a plane - a router's neck). A track holds
                  little heat next to a plane and how much imbalance a
                  track makes is folklore as a number (#328), so it is a
                  note, never charged.

Zone fills are read as the board saved them (the zone_fill family says
when those are stale).

CLI:
  check_tombstone.py --pcb board.kicad_pcb [--out r.json]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from shapely.geometry import Point
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402

SCRIPT = "check_tombstone"
SOURCE = SCRIPT
KIND = "dfm_tombstone"
KIND_TRACES = "dfm_tombstone_traces"
# 0603 lands sit ~1.5-1.65 mm centre to centre (IPC-7351 nominal), 0805's
# ~1.9: a chip with pads closer than this is 0603 or smaller
MAX_PITCH_MM = 1.75
MAX_PAD_MM = 1.2
BAND_MM = 0.1
RATIO = 3.0
DIFF_MM = 0.5
# a fill island smaller than this is a widened track (a router's neck or
# stub), not a plane that soaks heat
MIN_PLANE_MM2 = 5.0


def small_chips(bg) -> list[tuple[str, object, object]]:
    """(ref, pad a, pad b) for each 0603-or-smaller 2-pad SMD chip whose
    two pads are on different nets."""
    by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        by_ref.setdefault(p.ref, []).append(p)
    out = []
    for ref, pads in sorted(by_ref.items()):
        if "dnp" in (bg.footprints.get(ref) or {}).get("attrs", ()):
            continue
        cu = [p for p in pads if p.layers]
        if len(cu) != 2 or any(p.kind != "smd" for p in cu):
            continue
        a, b = cu
        if a.layers != b.layers or len(a.layers) != 1:
            continue
        if math.dist(a.center, b.center) > MAX_PITCH_MM:
            continue
        if max(*a.size, *b.size) > MAX_PAD_MM:
            continue
        if not a.net or not b.net or a.net == b.net:
            continue
        out.append((ref, a, b))
    return out


def _vias_to_plane(bg, pad, layer: str) -> list:
    """The vias inside pad whose barrel reaches a fill of the pad's net on
    another copper layer."""
    out = []
    for v in bg.vias_of(net=pad.net):
        if not pad.poly.contains(Point(v.at)):
            continue
        disc = v.poly
        if any(lay != layer and v.spans(lay)
               and z.fill_on(lay).intersects(disc)
               for z in bg.zones_of(net=pad.net) if not z.teardrop
               for lay in z.layers):
            out.append(v)
    return out


def thermal(pad, zone) -> bool:
    """Whether zone joins this SMD pad by thermal spokes: the pad's own
    zone_connect, else the zone's (thru_hole_only gives an SMD pad solid)."""
    c = pad.zone_connect if pad.zone_connect >= 0 else zone.connect
    return c == geom.ZONE_CONNECT_THERMAL


def _stitched(bg, island, net: str, layer: str) -> bool:
    """Whether a via of net inside the island runs down to a fill of net on
    another copper layer - the island is then the pad's way into a plane."""
    for v in bg.vias_of(net=net, layer=layer):
        if not island.intersects(v.poly):
            continue
        if any(lay != layer and v.spans(lay)
               and z.fill_on(lay).intersects(v.poly)
               for z in bg.zones_of(net=net) if not z.teardrop
               for lay in z.layers):
            return True
    return False


def end_copper(bg, pad, partner) -> dict:
    """The copper leaving one end: {"width": mm, "track": mm, "zones":
    {zone_id: mm}, "vias": [via, ...], "via_width": mm}. Same-net copper in
    a BAND_MM ring round the pad, by area / BAND_MM, plus pi x drill per via
    in the pad that reaches a plane."""
    layer = pad.layers[0]
    ring = pad.poly.buffer(BAND_MM, quad_segs=8).difference(pad.poly)
    zones: dict[int, object] = {}
    other = []
    for z in bg.zones_of(net=pad.net, layer=layer):
        f = z.fill_on(layer)
        if not f.is_empty and f.intersects(ring):
            # a teardrop is a zone KiCad builds round a track's end: track
            # copper, not a plane (#633); so is a pad joined by thermal
            # spokes (the relief is the cure, its spokes are track-like) and
            # so is a fill island too small to be a plane (a router's
            # "neck") unless a via in it runs down to a plane
            if z.teardrop or thermal(pad, z):
                other.append(f.intersection(ring))
                continue
            plane = []
            for g in getattr(f, "geoms", [f]):
                if not g.intersects(ring):
                    continue
                if g.area >= MIN_PLANE_MM2 or _stitched(bg, g, pad.net,
                                                        layer):
                    plane.append(g.intersection(ring))
                else:
                    other.append(g.intersection(ring))
            if plane:
                zones[z.zone_id] = unary_union(plane)
    for t in bg.tracks_of(net=pad.net, layer=layer):
        if t.poly.intersects(ring):
            other.append(t.poly.intersection(ring))
    for v in bg.vias_of(net=pad.net, layer=layer):
        if v.poly.intersects(ring):
            other.append(v.poly.intersection(ring))
    for p in bg.pads_of(net=pad.net, layer=layer):
        if p is not pad and p is not partner and p.poly.intersects(ring):
            other.append(p.poly.intersection(ring))
    drawn = unary_union(other) if other else None
    every = unary_union(list(zones.values()) + ([drawn] if drawn else []))
    vias = _vias_to_plane(bg, pad, layer)
    via_width = sum(math.pi * v.drill for v in vias)
    # a zone's share is the band copper only it covers: a track under a pour
    # is the pour's either way, so the plane takes what the two share
    zone_w = {zid: g.area / BAND_MM for zid, g in zones.items()}
    track_w = (drawn.difference(unary_union(list(zones.values()))).area
               if drawn is not None and zones else
               drawn.area if drawn is not None else 0.0) / BAND_MM
    return {"width": (every.area if not every.is_empty else 0.0) / BAND_MM
            + via_width, "track": track_w, "zones": zone_w, "vias": vias,
            "via_width": via_width}


def unequal(hi: float, lo: float) -> bool:
    return hi >= RATIO * lo and hi - lo >= DIFF_MM


def _r(x: float) -> float:
    return checklib.rnd(x, 2)


def check(bg, check: str = SCRIPT, source: str = SOURCE
          ) -> tuple[list[dict], dict]:
    """-> (violations, facts) for a geom.BoardGeom; dfm_check runs it as
    family `tombstone` with its own check name and source."""
    zone_by_id = {z.zone_id: z for z in bg.zones_of()}
    groups: dict[tuple, list[dict]] = {}
    vios: list[dict] = []
    chips = small_chips(bg)
    for ref, a, b in chips:
        ea, eb = end_copper(bg, a, b), end_copper(bg, b, a)
        (hp, he), (lp, le) = sorted(((a, ea), (b, eb)),
                                    key=lambda t: -t[1]["width"])
        if not unequal(he["width"], le["width"]):
            continue
        layer = hp.layers[0]
        hint = (f"{ref} ({hp.net} end {_r(he['width'])} mm of copper "
                f"leaving it, {lp.net} end {_r(le['width'])} mm)")
        common = {"pad": hp.number, "heavy_net": hp.net, "light_net": lp.net,
                  "heavy_mm": _r(he["width"]), "light_mm": _r(le["width"])}
        # info only when the tracks make the ends unequal and the plane
        # (fill joined solid, via in the pad) on its own would not
        if unequal(he["track"], le["width"]) and not unequal(
                he["width"] - he["track"], le["width"]):
            vios.append(checklib.violation(
                check, "info", hp.center, layer, hp.net, [ref],
                f"{hint}: the ends are unequal by track width (or thermal-"
                "relief spokes) - a part this "
                "small can stand up on the end that wets first; match the "
                "tracks if the part tombstones", source, kind=KIND_TRACES,
                **common))
            continue
        if he["zones"]:
            zid = max(he["zones"], key=he["zones"].get)
            groups.setdefault((zid, layer), []).append(
                {"ref": ref, "at": hp.center, "hint": hint, **common})
            continue
        drills = ", ".join(f"{v.drill:.2f}" for v in he["vias"])
        vios.append(checklib.violation(
            check, "warning", hp.center, layer, hp.net, [ref],
            f"{hint}: the via in pad {hp.number} ({drills} mm) runs down to "
            f"a {hp.net} plane, which pulls heat from that end at reflow "
            "and the part can stand up on the other - move the via out of "
            "the pad onto a thin track, or tie the other end as heavily",
            source, kind=KIND, **common))
    for (zid, layer), hits in sorted(groups.items(),
                                     key=lambda kv: (kv[0][1], kv[0][0])):
        z = zone_by_id.get(zid)
        zname = f"{z.net} pour" + (f" '{z.name}'" if z and z.name else "")
        refs = [h["ref"] for h in hits]
        v = checklib.violation(
            check, "warning", hits[0]["at"], layer, z.net, refs,
            f"the {zname} on {layer} joins one end of {len(hits)} 0603-or-"
            f"smaller chip(s) with far more copper than their other end "
            f"({', '.join(refs[:12])}{' ...' if len(refs) > 12 else ''}): "
            "the pour pulls heat from that end at reflow and the part can "
            "stand up on the other - give those pads a thermal relief "
            "(zone connect thermal), or match the other end",
            source, kind=KIND, zone=z.name or None,
            parts=[{k: h[k] for k in ("ref", "pad", "heavy_mm", "light_mm")}
                   for h in hits])
        v["items"] = [{"msg": h["hint"], "pos": [checklib.rnd(c) for c in h["at"]]}
                      for h in hits]
        vios.append(v)
    return vios, {"tombstone": {"small_chips": len(chips),
                                "unequal": sum(len(v.get("items") or [])
                                               for v in vios)}}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    vios, facts = check(geom.load_board(pcb))
    if not facts["tombstone"]["small_chips"]:
        facts["nothing_to_judge"] = "no 0603-or-smaller 2-pad SMD chips"
    return checklib.report(SCRIPT, pcb, vios, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
