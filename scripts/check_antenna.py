"""check_antenna.py - a radio module's PCB antenna has no copper under it and
sits at the board edge (P8 verify suite).

The hit: hifi-receiver-2's core passed every gate with GND and +3V3 planes
on all four layers, and two tracks, under about 5.4 mm of its ESP32-WROVER-E
antenna (review-2 DIGITAL-19, found by hand with pcbnew scripts). A plane
under a PCB inverted-F antenna detunes it and costs 10-20 dB; nothing in
ERC or DRC knows where a module's antenna is.

Which parts have a PCB antenna: reference/rf_modules.yaml (by Value, then
footprint name), or constraints.json {"antennas": {ref: "pcb" |
"external"}}. Where the antenna is (#1188): a row with a `keepout` carries
the module datasheet's antenna area (its depth from the module's antenna-end
edge, across the outline width) and any keep-out past the module (beyond_mm
out from that edge, side_mm each side); the area is placed on the module
outline, centred in the courtyard. A row without one, or a part the file
doesn't know: the module body (its courtyard) beyond the pad field (less
PAD_MARGIN_MM), on the end with the longest pad-free run, across the full
body width; the antenna end is that courtyard edge. A module with no
courtyard, or no pad-free end of MIN_END_MM, is reported, not guessed.

Findings, per module (Espressif ESP32 Hardware Design Guidelines §1.4.8, or
the row's datasheet):
  antenna_copper            error: copper of any kind (zone fill, track, via,
                            another part's pad) under the antenna keep-out on
                            a copper layer, inside the board: one per layer
  antenna_part              error: another part's body (its courtyard, less
                            PART_INSET_MM) under the antenna keep-out, on
                            either side of the board (#1188)
  antenna_inboard           warning: the antenna end is more than EDGE_MM
                            (the row's edge_mm) inside the board edge (the
                            guideline wants it over the edge, or at most 1 mm
                            in); a row's edge_mm: null skips it
  antenna_side_clearance    warning: copper closer than SIDE_MM beside the
                            antenna, along the board, where the board goes
                            on (the guideline's 15 mm clearance, or cut the
                            board away there)
  antenna_unknown_end       warning: a PCB-antenna module whose antenna end
                            can't be told from its footprint

A board's own antenna copper (IFA, meander, patch, chip antenna: #371) is
declared in constraints `pcb_antennas` [{name, nets | refs | poly, ...},
reference/constraints_schema.md]. There is no footprint to read it from, so
the row says what the copper is and what it copied. Findings:
  pcb_antenna_copper        error: other copper in its keep-out on any layer,
                            ground included (the short's own contact excepted)
  pcb_antenna_feed          error: the feed or short point is not on the
                            antenna's copper, or the short does not reach ground
  pcb_antenna_ground_edge   warning: radiator to ground edge off its height
  pcb_antenna_ground_small  warning: less ground than the reference assumed
  pcb_antenna_inboard       warning: radiator further from the board edge than
                            edge_max_mm
  pcb_antenna_geometry      error: the copper differs from the copied reference
  pcb_antenna_missing       error: the row names no copper on this board
  pcb_antenna_detuned, pcb_antenna_low_efficiency, pcb_antenna_sim_unconverged
                            warning: the row's `sim` (sim_solve antenna --out)
                            is off its band, under its floor, or not converged

CLI: --pcb board.kicad_pcb [--constraints c.json] [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402
import geom  # noqa: E402
import placelib  # noqa: E402

SCRIPT = "check_antenna"
SOURCE = SCRIPT  # verify_all, gate and the scorecard match a finding to its check by source
MODULES_YAML = HERE.parent / "reference" / "rf_modules.yaml"
ANTENNAS_YAML = HERE.parent / "reference" / "pcb_antennas.yaml"
EDGE_MM = 1.0       # antenna end at most this far inside the board edge
SIDE_MM = 15.0      # copper-free beside the antenna along the board
# the antenna end must be a pad-free run at least this long (mm): the
# ESP32/ESP8266 modules' are 6-7 mm, a pad row's own gap is well under 3
MIN_END_MM = 3.0
# the antenna stops this short of the pad field: the strip by the end pins
# is the module's ground end, and a pour up to its GND pads is normal
# (lite-main's ANT_KEEPOUT stops 0.42 mm short of them)
PAD_MARGIN_MM = 1.0
TOL_MM2 = 0.01      # copper under the antenna below this is arc sampling
# a part's courtyard counts under the antenna past this inset, so one drawn
# touching the module's courtyard (a 0.25 mm courtyard line) is not a hit
PART_INSET_MM = 0.25
# a courtyard this much shorter than the datasheet outline is another
# variant's footprint (WROOM-1U for WROOM-1): the datasheet area isn't placed
OUTLINE_SLACK_MM = 2.0


def load_modules(path: Path = MODULES_YAML) -> list[dict]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for m in doc.get("modules") or []:
        out.append({**m, "_match": re.compile(m["match"], re.I),
                    "_external": re.compile(m.get("external") or "$^", re.I)})
    return out


def antenna_kind(value: str, fpid: str, modules: list[dict]) -> tuple:
    """("pcb" | "external" | None, the module row). The Value decides when
    it names a known module; the footprint name only when it doesn't."""
    for text in (value or "", fpid or ""):
        for m in modules:
            if m["_match"].search(text):
                return ("external" if m["_external"].search(text) else "pcb",
                        m)
    return None, None


def board_area(bg) -> "object":
    """The board outline less the windows cut out of it."""
    out = bg.outline
    for face in getattr(bg, "outline_faces", [])[1:]:
        if out.contains(face.representative_point()) and \
                face.area < out.area:
            out = out.difference(face)
    return out


def _to_local(g, f):
    return affinity.rotate(affinity.translate(g, -f.pos[0], -f.pos[1]),
                           f.angle, origin=(0, 0))


def _to_board(g, f):
    return affinity.translate(affinity.rotate(g, -f.angle, origin=(0, 0)),
                              f.pos[0], f.pos[1])


def _ends(f, pads):
    """The courtyard's bounds and, per end ("-y" ...), the pad-free run from
    that courtyard edge to the pad field, all in the local frame."""
    cx0, cy0, cx1, cy1 = f.courtyard_local.bounds
    px0, py0, px1, py1 = unary_union(
        [_to_local(p.poly, f) for p in pads]).bounds
    return (cx0, cy0, cx1, cy1), (px0, py0, px1, py1), {
        "-y": py0 - cy0, "+y": cy1 - py1, "-x": px0 - cx0, "+x": cx1 - px1}


def antenna_geometry(f, pads, row: dict | None = None) -> dict | None:
    """The antenna rectangle, its end edge and the two side bands, in the
    board frame, or None when the footprint has no pad-free end. With a row
    `keepout` (the module datasheet's antenna area) the area is the
    datasheet's, placed on the module outline; `keepout` is the antenna area
    grown by the datasheet's extension past the module."""
    if f.courtyard_local is None or f.courtyard_missing or not pads:
        return None
    ko = (row or {}).get("keepout")
    mismatch = False
    if ko:
        g = _datasheet_geometry(f, pads, row, ko)
        if g is not None:
            return g
        mismatch = True
    (cx0, cy0, cx1, cy1), (px0, py0, px1, py1), ends = _ends(f, pads)
    side, depth = max(ends.items(), key=lambda kv: kv[1])
    depth -= PAD_MARGIN_MM
    if depth < MIN_END_MM:
        return None
    m = PAD_MARGIN_MM
    px0, py0, px1, py1 = px0 - m, py0 - m, px1 + m, py1 + m
    s = SIDE_MM
    if side == "-y":
        ant, end = box(cx0, cy0, cx1, py0), box(cx0, cy0, cx1, cy0)
        bands = [box(cx0 - s, cy0, cx0, py0), box(cx1, cy0, cx1 + s, py0)]
    elif side == "+y":
        ant, end = box(cx0, py1, cx1, cy1), box(cx0, cy1, cx1, cy1)
        bands = [box(cx0 - s, py1, cx0, cy1), box(cx1, py1, cx1 + s, cy1)]
    elif side == "-x":
        ant, end = box(cx0, cy0, px0, cy1), box(cx0, cy0, cx0, cy1)
        bands = [box(cx0, cy0 - s, px0, cy0), box(cx0, cy1, px0, cy1 + s)]
    else:
        ant, end = box(px1, cy0, cx1, cy1), box(cx1, cy0, cx1, cy1)
        bands = [box(px1, cy0 - s, cx1, cy0), box(px1, cy1, cx1, cy1 + s)]
    ant_b = _to_board(ant, f)
    return {"antenna": ant_b, "keepout": ant_b, "end": _to_board(end, f),
            "bands": [_to_board(b, f) for b in bands], "depth": depth,
            "body": _to_board(f.courtyard_local, f),
            "from": "footprint (outline mismatch)" if mismatch
            else "footprint"}


def _datasheet_geometry(f, pads, row, ko) -> dict | None:
    """The row's keep-out on this footprint. The antenna end is one of the
    two ends of the axis the outline's length runs along (the courtyard's
    longer extent matched to outline_mm): the one by pad 1 (`end: pin1`), the
    one away from it (`opposite_pin1`), or the longer pad-free run
    (`pad_free`). The module edge is the courtyard edge moved in by half the
    courtyard's excess over the outline length. None when the courtyard is
    shorter than the outline by more than OUTLINE_SLACK_MM along its length
    (a value naming the module on another variant's footprint, such as a
    WROOM-1 value on a WROOM-1U footprint): the caller falls back to the
    footprint's pad-free end."""
    (cx0, cy0, cx1, cy1), _, ends = _ends(f, pads)
    width, length = (float(v) for v in ko["outline_mm"])
    ex, ey = cx1 - cx0, cy1 - cy0
    along_x = abs(ex - length) + abs(ey - width) < \
        abs(ey - length) + abs(ex - width)
    if (ex if along_x else ey) < length - OUTLINE_SLACK_MM:
        return None
    cand = ("-x", "+x") if along_x else ("-y", "+y")
    how = ko.get("end", "pad_free")
    if how in ("pin1", "opposite_pin1"):
        one = [p for p in pads if str(p.number) == "1"]
        if not one:
            return None
        c = _to_local(one[0].poly, f).centroid
        x, y = c.x, c.y
        q = x if along_x else y
        lo_e, hi_e = (cx0, cx1) if along_x else (cy0, cy1)
        near = cand[0] if q - lo_e <= hi_e - q else cand[1]
        side = near if how == "pin1" else (cand[1] if near == cand[0]
                                           else cand[0])
    else:
        side = max(cand, key=lambda c: ends[c])
    ext = ex if along_x else ey
    inset = max(0.0, (ext - length) / 2)
    depth = float(ko["antenna_mm"])
    beyond = float(ko.get("beyond_mm") or 0.0)
    wide = float(ko.get("side_mm") or 0.0)
    s = row.get("side_clear_mm", SIDE_MM)
    s = 0.0 if s is None else float(s)
    # the across-axis centre and half width of the outline
    if along_x:
        mid, half = (cy0 + cy1) / 2, width / 2
    else:
        mid, half = (cx0 + cx1) / 2, width / 2
    if side in ("-x", "-y"):
        e = (cx0 if side == "-x" else cy0) + inset
        a0, a1, k0 = e, e + depth, e - beyond
    else:
        e = (cx1 if side == "+x" else cy1) - inset
        a0, a1, k0 = e - depth, e, e + beyond
    lo, hi = mid - half, mid + half

    def rect(u0, u1, v0, v1):
        u0, u1 = min(u0, u1), max(u0, u1)
        return box(u0, v0, u1, v1) if along_x else box(v0, u0, v1, u1)
    ant = rect(a0, a1, lo, hi)
    keep = rect(k0, a1 if side in ("-x", "-y") else a0, lo - wide, hi + wide)
    end = rect(e, e, lo, hi)
    bands = [rect(a0, a1, lo - s, lo), rect(a0, a1, hi, hi + s)] if s else []
    return {"antenna": _to_board(ant, f), "keepout": _to_board(keep, f),
            "end": _to_board(end, f), "bands": [_to_board(b, f) for b in bands],
            "depth": depth, "body": _to_board(f.courtyard_local, f),
            "from": "datasheet"}


def copper_in(bg, region, layer: str, skip_ref: str) -> dict:
    """Every copper item on `layer` that reaches into `region`."""
    found = {"shapes": [], "nets": set(), "tracks": 0, "vias": 0, "pads": [],
             "zones": 0}
    if region.is_empty:
        return found
    for z in bg.zones_of(layer=layer):
        hit = z.fill_on(layer).intersection(region)
        if hit.area > TOL_MM2:
            found["shapes"].append(hit)
            found["nets"].add(z.net)
            found["zones"] += 1
    for t in bg.tracks_of(layer=layer):
        if t.poly.intersects(region):
            found["shapes"].append(t.poly.intersection(region))
            found["nets"].add(t.net)
            found["tracks"] += 1
    for v in bg.vias_of(layer=layer):
        if v.poly.intersects(region):
            found["shapes"].append(v.poly.intersection(region))
            found["nets"].add(v.net)
            found["vias"] += 1
    for p in bg.pads_of(layer=layer):
        if p.ref != skip_ref and p.poly.intersects(region):
            found["shapes"].append(p.poly.intersection(region))
            found["nets"].add(p.net or "(no net)")
            found["pads"].append(f"{p.ref}.{p.number}")
    return found


def _overrides(constraints: dict | None) -> dict:
    ants = (constraints or {}).get("antennas") or {}
    bad = {r: k for r, k in ants.items() if k not in ("pcb", "external")}
    if bad:
        raise checklib.CheckError(
            f"constraints antennas: {bad} - each must be 'pcb' or "
            "'external'")
    return ants


def check(pcb: Path, constraints: dict | None = None,
          modules: list[dict] | None = None) -> tuple[list[dict], dict]:
    modules = load_modules() if modules is None else modules
    overrides = _overrides(constraints)
    bg = geom.load_board(pcb)
    model = placelib.PlaceModel(pcb)
    board = board_area(bg)
    viols, seen = [], []
    for ref in sorted(model.footprints):
        f = model.footprints[ref]
        value = (bg.footprints.get(ref) or {}).get("value") or ""
        kind, row = antenna_kind(value, f.fpid, modules)
        kind = overrides.get(ref, kind)
        if kind is None:
            continue
        fact = {"ref": ref, "value": value, "footprint": f.fpid,
                "antenna": kind}
        seen.append(fact)
        if kind != "pcb":
            continue
        g = antenna_geometry(f, bg.pads_of(ref=ref), row)
        if g is None:
            viols.append(checklib.violation(
                SCRIPT, "warning", f.pos, None, None, [ref],
                f"{ref} ({value or f.fpid}) has a PCB antenna, but its "
                f"footprint has no courtyard with a pad-free end of "
                f"{MIN_END_MM:g} mm or more, so the antenna can't be placed: "
                "check its keep-out by hand", SOURCE,
                kind="antenna_unknown_end"))
            continue
        ant = g["antenna"]
        fact.update(depth_mm=checklib.rnd(g["depth"], 2),
                    antenna_bbox=[checklib.rnd(v, 3) for v in ant.bounds],
                    antenna_from=g["from"])
        if g["keepout"] is not ant:
            fact["keepout_bbox"] = [checklib.rnd(v, 3)
                                    for v in g["keepout"].bounds]
        over = g["keepout"].intersection(board)
        fact["over_board_mm2"] = checklib.rnd(over.area, 2)
        for layer in bg.copper_layers:
            found = copper_in(bg, over, layer, ref)
            if not found["shapes"]:
                continue
            hit = unary_union(found["shapes"])
            if hit.area <= TOL_MM2:
                continue
            parts = []
            if found["zones"]:
                parts.append(f"{found['zones']} zone fill(s)")
            if found["tracks"]:
                parts.append(f"{found['tracks']} track(s)")
            if found["vias"]:
                parts.append(f"{found['vias']} via(s)")
            if found["pads"]:
                parts.append("pads " + ", ".join(sorted(found["pads"])))
            nets = sorted(n for n in found["nets"] if n)
            c = hit.centroid
            viols.append(checklib.violation(
                SCRIPT, "error", (c.x, c.y), layer,
                nets[0] if len(nets) == 1 else None, [ref],
                f"{ref} ({value or f.fpid}): {hit.area:.1f} mm2 of copper "
                f"on {layer} under its PCB antenna ({'; '.join(parts)}; "
                f"nets {', '.join(nets)}). Keep the antenna area free of "
                "copper on every layer (a keepout rule area, or cut the "
                "board away / move the module so the antenna overhangs)",
                SOURCE, kind="antenna_copper", nets=nets,
                area_mm2=checklib.rnd(hit.area, 2),
                antenna_bbox=fact["antenna_bbox"]))
        viols += _parts_under(model, ref, value or f.fpid, over, g["body"])
        # the antenna end against the board edge
        edge_mm = (row or {}).get("edge_mm", EDGE_MM)
        mid = g["end"].centroid
        if edge_mm is None:
            pass
        elif board.contains(mid):
            inset = board.exterior.distance(mid)
            fact["end_inside_mm"] = checklib.rnd(inset, 2)
            if inset > edge_mm:
                viols.append(checklib.violation(
                    SCRIPT, "warning", (mid.x, mid.y), None, None, [ref],
                    f"{ref}: the antenna end is {inset:.1f} mm inside the "
                    f"board edge (at most {edge_mm:g} mm, or over the edge): "
                    "move the module to the edge or cut the board away "
                    "around the antenna", SOURCE, kind="antenna_inboard",
                    inset_mm=checklib.rnd(inset, 2)))
        else:
            fact["end_inside_mm"] = 0.0
        # copper beside the antenna, where the board goes on
        cleared = []
        for band in g["bands"]:
            on = band.intersection(board).difference(g["body"])
            if on.area <= TOL_MM2:
                continue
            near = [unary_union(copper_in(bg, on, layer, ref)["shapes"])
                    for layer in bg.copper_layers]
            near = [n for n in near if not n.is_empty and n.area > TOL_MM2]
            if near:
                cleared.append(min(ant.distance(n) for n in near))
        fact["side_cleared_mm"] = [checklib.rnd(d, 2) for d in cleared]
        side_mm = (row or {}).get("side_clear_mm", SIDE_MM)
        short = [d for d in cleared if side_mm is not None and d < side_mm]
        if short and over.area > TOL_MM2:
            viols.append(checklib.violation(
                SCRIPT, "warning", (ant.centroid.x, ant.centroid.y), None,
                None, [ref],
                f"{ref}: copper starts {min(short):.1f} mm beside the "
                f"antenna (Espressif asks for {side_mm:g} mm clear along the "
                "edge, or the board cut away there): widen the keepout or "
                "notch the board", SOURCE, kind="antenna_side_clearance",
                cleared_mm=[checklib.rnd(d, 2) for d in short]))
        if row:
            fact["source"] = row.get("source")
            if row.get("keepout"):
                fact["keepout_source"] = row["keepout"].get("source")
    return viols, {"modules": seen}


def _parts_under(model, ref: str, name: str, over, body) -> list[dict]:
    """Another part's body under the antenna keep-out, on either side: its
    courtyard (less PART_INSET_MM, so a courtyard drawn against the module's
    doesn't count) over the keep-out inside the board. Copper is
    antenna_copper's; this is the part's body, a dielectric and often metal
    (a can, a crystal, a connector shell) in the antenna's near field. A
    courtyard that holds the whole module's is not a part under it but an
    outline around it (mdbt-micro-50q's J1 "ProMicro" is the board's own
    pin-out drawn as a footprint), so it is skipped; so is one on the
    module's side that holds the module's centre, since no real part can sit
    there (that J1 again, where the module's courtyard pokes past it)."""
    out = []
    if over.is_empty or over.area <= TOL_MM2:
        return out
    side = model.footprints[ref].side
    for other in sorted(model.footprints):
        o = model.footprints[other]
        if other == ref or "board_only" in o.attrs or not o.pads or \
                o.courtyard_local is None or o.courtyard_missing:
            continue
        cy = _to_board(o.courtyard_local, o).buffer(-PART_INSET_MM)
        if cy.is_empty or not cy.intersects(over):
            continue
        if cy.buffer(PART_INSET_MM).contains(body.buffer(-PART_INSET_MM)) \
                or (o.side == side and cy.contains(body.centroid)):
            continue
        hit = cy.intersection(over)
        if hit.area <= TOL_MM2:
            continue
        c = hit.centroid
        out.append(checklib.violation(
            SCRIPT, "error", (c.x, c.y), None, None, [ref, other],
            f"{ref} ({name}): {other} ({o.fpid}, {o.side}) sits "
            f"{hit.area:.1f} mm2 under its antenna keep-out. No parts under "
            "the antenna on either side: move it clear of the keep-out",
            SOURCE, kind="antenna_part", part=other, side=o.side,
            area_mm2=checklib.rnd(hit.area, 2)))
    return out


# ------------------------------------------------- a board's own antenna copper

def _aliases(names) -> set:
    """Net names as a row may write them: KiCad's sheet slash is optional."""
    names = {str(n) for n in names or ()}
    return names | {n.lstrip("/") for n in names} | {"/" + n.lstrip("/") for n in names}


def _copper_pads(bg, layer: str):
    """The pads on `layer` that hold copper. An unplated hole no wider than
    its pad's copper size (a mouse bite, `size 0.5` with `drill 0.5`) names the
    copper layers yet leaves no metal round it."""
    return [p for p in bg.pads_of(layer=layer)
            if not (p.kind == "np_thru_hole" and p.drill
                    and min(p.size) <= min(p.drill))]


def _row_copper(bg, row: dict, layer: str):
    """The antenna's copper on `layer`: its nets, its footprints' pads, and
    whatever copper a poly touches."""
    nets, refs = _aliases(row.get("nets")), set(row.get("refs") or ())
    poly = Polygon(row["poly"]) if row.get("poly") else None
    out = []

    def take(net, ref, g):
        if g is None or g.is_empty:
            return
        if net in nets or ref in refs:
            out.append(g)
        elif poly is not None and poly.intersects(g):
            out.append(_areal(g.intersection(poly)))
    for t in bg.tracks_of(layer=layer):
        take(t.net, None, t.poly)
    for v in bg.vias_of(layer=layer):
        take(v.net, None, v.poly)
    for p in _copper_pads(bg, layer):
        take(p.net, p.ref, p.poly)
    for z in bg.zones_of(layer=layer):
        take(z.net, None, z.fill_on(layer))
    out = [g for g in out if not g.is_empty and g.area > 0]
    return unary_union(out) if out else None


def _ground(bg, nets, layers):
    out = {}
    for layer in layers:
        items = [t.poly for t in bg.tracks_of(layer=layer) if t.net in nets]
        items += [p.poly for p in bg.pads_of(layer=layer) if p.net in nets]
        items += [v.poly for v in bg.vias_of(layer=layer) if v.net in nets]
        items += [z.fill_on(layer) for z in bg.zones_of(layer=layer) if z.net in nets]
        items = [g for g in items if g is not None and not g.is_empty]
        if items:
            out[layer] = unary_union(items)
    return out


def _vertices(g):
    pts = []
    for part in (g.geoms if hasattr(g, "geoms") else [g]):
        pts += list(part.exterior.coords)
    return pts


def _reference_row(ref):
    """A reference given inline, or by name from reference/pcb_antennas.yaml."""
    if isinstance(ref, dict):
        return ref
    doc = yaml.safe_load(ANTENNAS_YAML.read_text(encoding="utf-8")) or {}
    rows = doc.get("antennas") or {}
    if ref not in rows:
        raise checklib.CheckError(f"pcb_antennas reference {ref!r}: not in {ANTENNAS_YAML.name} "
                                  f"(has {', '.join(sorted(rows))})")
    return rows[ref]


def _placed(ref: dict, row: dict):
    """The reference's copper in board coordinates: rotated about its anchor
    (rot, degrees), the anchor put on the row's feed."""
    pieces = [Polygon(p) for p in ref["polys"]]
    g = unary_union([p.buffer(0) for p in pieces])
    ax, ay = ref.get("anchor") or (0.0, 0.0)
    g = affinity.translate(g, -ax, -ay)
    g = affinity.rotate(g, float(row.get("rot") or 0.0), origin=(0, 0))
    return affinity.translate(g, *row["feed"])


def _sim_findings(row: dict, base: Path, name: str, at, viols: list, fact: dict, not_judged: list) -> None:
    sim = row.get("sim")
    if not sim:
        not_judged.append(f"{name}: no sim_solve antenna result (`sim`), so its tuning is not judged")
        return
    path = Path(sim) if Path(sim).is_absolute() else base / sim
    if not path.is_file():
        not_judged.append(f"{name}: {sim} not found, so its tuning is not judged")
        return
    r = checklib.load_json(str(path), "sim")
    ft = r.get("features") or {}
    band = row.get("band_hz") or r.get("band")
    fact["sim"] = {"f_res": ft.get("f_res"), "s11_db": ft.get("s11_db"), "bw_10db": ft.get("bw_10db")}
    if band and ft.get("f_res"):
        bw = ft.get("bw_10db") or [ft["f_res"], ft["f_res"]]
        width = max(bw[1] - bw[0], 0.0)
        mid, half = (band[0] + band[1]) / 2, (band[1] - band[0]) / 2
        off = abs(ft["f_res"] - mid) - half
        if off > width or (ft.get("s11_db") or 0) > -10:
            viols.append(checklib.violation(
                SCRIPT, "warning", at, row.get("layer"), None, row.get("refs") or [],
                f"{name}: resonates at {ft['f_res'] / 1e9:.3f} GHz (S11 {ft.get('s11_db')} dB, "
                f"-10 dB width {width / 1e6:.0f} MHz) against the band "
                f"{band[0] / 1e9:.3f}-{band[1] / 1e9:.3f} GHz: retune it or fit a match",
                SOURCE, kind="pcb_antenna_detuned", f_res=ft["f_res"], band_hz=band))
    floor = row.get("min_efficiency")
    far = r.get("at_band") or {}
    if floor is not None and far.get("efficiency") is not None and far["efficiency"] < floor:
        viols.append(checklib.violation(
            SCRIPT, "warning", at, row.get("layer"), None, row.get("refs") or [],
            f"{name}: radiation efficiency {far['efficiency']:.2f} under the stated {floor:g}",
            SOURCE, kind="pcb_antenna_low_efficiency", efficiency=far["efficiency"]))
    conv = r.get("convergence")
    if not conv or not conv.get("converged"):
        viols.append(checklib.violation(
            SCRIPT, "warning", at, row.get("layer"), None, row.get("refs") or [],
            f"{name}: {sim} has no converged mesh study (run sim_solve antenna --converge), "
            "so its numbers are not to be relied on",
            SOURCE, kind="pcb_antenna_sim_unconverged"))


def check_pcb_antennas(pcb: Path, bg, board, rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    viols, facts, not_judged = [], [], []
    for row in rows:
        name = row.get("name") or "?"
        if not (row.get("nets") or row.get("refs") or row.get("poly")):
            raise checklib.CheckError(f"pcb_antennas {name}: needs nets, refs or poly")
        layers = [row["layer"]] if row.get("layer") else list(bg.copper_layers)
        radiator = {l: _row_copper(bg, row, l) for l in bg.copper_layers}
        radiator = {l: g for l, g in radiator.items() if g is not None and not g.is_empty and g.area > TOL_MM2}
        rad_all = unary_union(list(radiator.values())) if radiator else None
        fact = {"name": name, "layers": sorted(radiator)}
        facts.append(fact)
        if rad_all is None:
            viols.append(checklib.violation(
                SCRIPT, "error", None, None, None, row.get("refs") or [],
                f"{name}: names no copper on this board ({layers})", SOURCE,
                kind="pcb_antenna_missing"))
            continue
        c = rad_all.centroid
        at = (c.x, c.y)
        fact["bbox"] = [checklib.rnd(v, 3) for v in rad_all.bounds]
        ground_nets = _aliases(row.get("ground_nets") or ["GND"])
        ground = _ground(bg, ground_nets, bg.copper_layers)
        gnd_all = unary_union(list(ground.values())) if ground else None

        # keep-out: the row's poly, else the radiator's box grown by keepout_mm,
        # on every layer. The arms land on the ground, so a box drawn from
        # the copper overlaps it: with a feed and a short side by side the box
        # stops short_contact_mm before their line. over_ground (a patch):
        # ground under it is the design.
        margin = float(row.get("keepout_mm", 0.0))
        keep = (Polygon(row["keepout"]) if row.get("keepout") else box(*rad_all.bounds).buffer(margin, join_style=2))
        if not row.get("keepout") and row.get("feed") and row.get("short"):
            keep = _trim_at_contacts(keep, rad_all, row["feed"], row["short"],
                                     float(row.get("short_contact_mm", 1.5)))
        keep = keep.intersection(board)
        own_nets = _aliases(row.get("nets"))
        own_refs = set(row.get("refs") or ())
        if row.get("over_ground"):
            own_nets = own_nets | ground_nets
        for layer in bg.copper_layers:
            found = _foreign_copper(bg, keep, layer, own_nets, own_refs, radiator.get(layer))
            found = [g for g in found if not g.is_empty and g.area > TOL_MM2]
            if not found:
                continue
            hit = unary_union(found)
            if hit.area <= TOL_MM2:
                continue
            ct = hit.centroid
            viols.append(checklib.violation(
                SCRIPT, "error", (ct.x, ct.y), layer, None, sorted(own_refs),
                f"{name}: {hit.area:.1f} mm2 of other copper on {layer} in its keep-out "
                f"{[checklib.rnd(v, 2) for v in keep.bounds]}: an antenna's near field "
                "lands on it and detunes it. Clear it on every layer, or declare a "
                "smaller `keepout` if the reference design has it there",
                SOURCE, kind="pcb_antenna_copper", area_mm2=checklib.rnd(hit.area, 2)))

        # the feed and the short
        rad_b = rad_all.buffer(0.01)
        if row.get("feed"):
            pt = Point(row["feed"])
            if not rad_b.contains(pt):
                viols.append(checklib.violation(
                    SCRIPT, "error", tuple(row["feed"]), None, None, sorted(own_refs),
                    f"{name}: the feed point {row['feed']} is not on the antenna's copper "
                    f"({checklib.rnd(rad_all.distance(pt), 3)} mm off)",
                    SOURCE, kind="pcb_antenna_feed"))
        else:
            not_judged.append(f"{name}: no `feed` point")
        if row.get("short"):
            pt = Point(row["short"])
            if not rad_b.contains(pt):
                viols.append(checklib.violation(
                    SCRIPT, "error", tuple(row["short"]), None, None, sorted(own_refs),
                    f"{name}: the short point {row['short']} is not on the antenna's copper",
                    SOURCE, kind="pcb_antenna_feed"))
            elif gnd_all is None or gnd_all.distance(pt) > float(row.get("short_reach_mm", 1.5)):
                dist = checklib.rnd(gnd_all.distance(pt), 3) if gnd_all is not None else None
                viols.append(checklib.violation(
                    SCRIPT, "error", tuple(row["short"]), None, None, sorted(own_refs),
                    f"{name}: the short does not reach ground {row.get('ground_nets') or ['GND']} "
                    f"(nearest {dist} mm): an open IFA is another antenna",
                    SOURCE, kind="pcb_antenna_feed", distance_mm=dist))

        # the ground edge: the radiator's farthest copper from the nearest ground
        ge = row.get("ground_edge") or {}
        if gnd_all is not None:
            height = max(gnd_all.distance(Point(p)) for p in _vertices(rad_all))
            fact["height_mm"] = checklib.rnd(height, 3)
            if ge.get("height_mm") is not None:
                tol = float(ge.get("tol_mm", 0.25))
                if abs(height - ge["height_mm"]) > tol:
                    viols.append(checklib.violation(
                        SCRIPT, "warning", at, None, None, sorted(own_refs),
                        f"{name}: its copper reaches {height:.2f} mm from the ground edge, "
                        f"the design says {ge['height_mm']:g} +- {tol:g} mm",
                        SOURCE, kind="pcb_antenna_ground_edge", height_mm=checklib.rnd(height, 3)))
        elif ge:
            not_judged.append(f"{name}: no copper on ground nets {row.get('ground_nets') or ['GND']}, so no ground edge")

        # the board edge
        if row.get("edge_max_mm") is not None:
            ring = board.exterior
            d = min(ring.distance(Point(p)) for p in _vertices(rad_all))
            fact["edge_mm"] = checklib.rnd(d, 3)
            if d > float(row["edge_max_mm"]):
                viols.append(checklib.violation(
                    SCRIPT, "warning", at, None, None, sorted(own_refs),
                    f"{name}: its copper comes no closer than {d:.1f} mm to the board edge "
                    f"(at most {row['edge_max_mm']:g} mm): the board around it loads it",
                    SOURCE, kind="pcb_antenna_inboard", edge_mm=checklib.rnd(d, 3)))

        # the copied reference
        if row.get("reference"):
            ref = _reference_row(row["reference"])
            fact["reference"] = ref.get("source") or row["reference"]
            gsize = ref.get("ground_mm")
            if gsize and gnd_all is not None:
                bx = gnd_all.bounds
                dims = sorted((bx[2] - bx[0], bx[3] - bx[1]))
                fact["ground_mm"] = [checklib.rnd(v, 2) for v in dims]
                if dims[0] < min(gsize) - 0.5 or dims[1] < max(gsize) - 0.5:
                    viols.append(checklib.violation(
                        SCRIPT, "warning", at, None, None, sorted(own_refs),
                        f"{name}: the ground is {dims[0]:.1f} x {dims[1]:.1f} mm, "
                        f"{ref.get('source') or 'the reference'} assumed {gsize}: its tuning "
                        "does not carry over without a solve (sim_solve antenna)",
                        SOURCE, kind="pcb_antenna_ground_small", ground_mm=[checklib.rnd(v, 2) for v in dims]))
            if ref.get("polys"):
                if not row.get("feed"):
                    not_judged.append(f"{name}: reference geometry needs a `feed` point to anchor on")
                else:
                    want = _placed(ref, row)
                    diff = want.symmetric_difference(rad_all)
                    pct = 100 * diff.area / want.area if want.area else 100.0
                    tol_pct = float(row.get("geometry_tol_pct", ref.get("area_tol_pct", 5.0)))
                    fact["geometry_diff_pct"] = checklib.rnd(pct, 2)
                    if pct > tol_pct:
                        ct = diff.centroid
                        viols.append(checklib.violation(
                            SCRIPT, "error", (ct.x, ct.y), None, None, sorted(own_refs),
                            f"{name}: the copper differs from {ref.get('source') or 'the reference'} "
                            f"by {pct:.1f}% of its area (limit {tol_pct:g}%; {diff.area:.1f} mm2): "
                            "a copied antenna keeps its tuning only if it is the same copper",
                            SOURCE, kind="pcb_antenna_geometry", diff_pct=checklib.rnd(pct, 2)))
        _sim_findings(row, Path(pcb).parent, name, at, viols, fact, not_judged)
    return viols, facts, not_judged


def _trim_at_contacts(keep, rad, feed, short, margin):
    """The keep-out cut at the line through feed and short, less margin, on the
    side with more of the radiator. Only when they sit on an axis."""
    dx, dy = abs(feed[0] - short[0]), abs(feed[1] - short[1])
    x0, y0, x1, y1 = keep.bounds
    if dy <= 1e-3 < dx:
        c = (feed[1] + short[1]) / 2
        lo, hi = box(x0, y0, x1, c - margin), box(x0, c + margin, x1, y1)
    elif dx <= 1e-3 < dy:
        c = (feed[0] + short[0]) / 2
        lo, hi = box(x0, y0, c - margin, y1), box(c + margin, y0, x1, y1)
    else:
        return keep
    side = lo if rad.intersection(lo).area >= rad.intersection(hi).area else hi
    return keep.intersection(side)


def _areal(g):
    """g's parts with area: a keep-out's edge running along copper leaves a
    mixed collection, and GEOS refuses to overlay those."""
    parts = getattr(g, "geoms", None)
    if g.geom_type != "GeometryCollection" or parts is None:
        return g
    return unary_union([p for p in parts if p.area > 0])


def _foreign_copper(bg, region, layer: str, own_nets, own_refs, rad) -> list:
    """Copper on `layer` in `region` that isn't the antenna's own."""
    out = []
    if region.is_empty:
        return out
    items = [(t.net, None, t.poly) for t in bg.tracks_of(layer=layer)]
    items += [(v.net, None, v.poly) for v in bg.vias_of(layer=layer)]
    items += [(p.net, p.ref, p.poly) for p in _copper_pads(bg, layer)]
    items += [(z.net, None, z.fill_on(layer)) for z in bg.zones_of(layer=layer)]
    for net, ref, g in items:
        if g is None or g.is_empty or net in own_nets or ref in own_refs:
            continue
        hit = _areal(g.intersection(region))
        if rad is not None:
            hit = hit.difference(rad)
        if not hit.is_empty:
            out.append(hit)
    return out


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = (checklib.load_json(args.constraints, "constraints")
            if args.constraints else None)
    viols, seen = check(pcb, cons)
    rows = (cons or {}).get("pcb_antennas") or []
    not_judged = []
    if rows:
        bg = geom.load_board(pcb)
        more, seen["pcb_antennas"], not_judged = check_pcb_antennas(pcb, bg, board_area(bg), rows)
        viols += more
    return checklib.report(SCRIPT, pcb, viols, checked=[seen],
                           nothing_to_judge=None if seen.get("modules") or rows else
                           "no module reference/rf_modules.yaml knows, and no pcb_antennas row",
                           not_judged=not_judged), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
