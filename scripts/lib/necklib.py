"""necklib - where a power net may run at its courtyard-held neck width (#924, #937).

rules_gen's aiee_pwr_neck lets a power net narrow inside the courtyard of a
part whose pads are narrower than the net's width rule. route_finish read
those rules first (#924); route_escape and route_auto read them here too, so
a fine-pitch power ball gets out at every stage, not only at close.

board_necks(pcb, bg, neck_rules) -> {net: {layer: [(zone, width)]}}. A zone
is where a track at the rule's width meets the footprint's courtyard: KiCad's
intersectsCourtyard reads the track's copper, not its centre line, so the
courtyard grows by half that width, less NECK_OVERLAP_MM (#1140: the way from
C216's +3V3 pad to its via ends 0.02 mm past the courtyard edge, where the
bare courtyard held it to the net's full width). The zone is cut to
NECK_REACH_MM round the net's own pads of that part on the layer, so a neck
stays the pad's escape check_current accepts (its 1 mm pad-exit bound), not
a narrow trace across the part.

neck_width(necks, net, layer, shape) -> the narrowest neck width whose zone
holds the whole shape (a point, a track's centre line), or None.
"""
from __future__ import annotations

import sys
from pathlib import Path

import shapely
from shapely import affinity

NECK_REACH_MM = 0.75          # #924, how far past its pads a neck may run:
#                               inside check_current's 1 mm pad exit
NECK_OVERLAP_MM = 0.02        # #1140, how far a neck track's copper still
#                               reaches into the courtyard at the zone's edge


def courtyard_abs(f):
    """A placelib footprint's declared courtyard in the board frame, or None
    (check_courtyard's reading)."""
    if f is None or f.courtyard_local is None:
        return None
    p = affinity.rotate(f.courtyard_local, -f.angle, origin=(0, 0))
    return affinity.translate(p, f.pos[0], f.pos[1])


def board_necks(pcb: Path, bg, neck_rules: list) -> dict:
    """{net: {layer: [(zone, width)]}} for the .kicad_dru's courtyard-held
    width rules (route_critical.dru_neck_rules); {} without any. A centre
    line inside a zone keeps a track of the zone's width on the courtyard."""
    if not neck_rules:
        return {}
    import placelib
    fps = placelib.PlaceModel(pcb).footprints
    out: dict = {}
    for r in neck_rules:
        cy = courtyard_abs(fps.get(r["ref"]))
        if cy is None or cy.is_empty:
            continue
        held = cy.buffer(max(r["min_mm"] / 2 - NECK_OVERLAP_MM, 0.0))
        for layer in bg.copper_layers:
            pads = [p.poly for p in bg.pads_of(r["net"], layer)
                    if p.ref == r["ref"]]
            if not pads:
                continue
            zone = held.intersection(
                shapely.union_all(pads).buffer(NECK_REACH_MM))
            if not zone.is_empty:
                out.setdefault(r["net"], {}).setdefault(layer, []).append(
                    (zone, r["min_mm"]))
    return out


def read_necks(pcb: Path, bg=None) -> dict:
    """board_necks of a board file, its rules read from the .kicad_dru
    beside it."""
    scripts_dir = Path(__file__).resolve().parents[1]   # python -I
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import geom
    import route_critical as rc
    pcb = Path(pcb)
    rules = rc.dru_neck_rules(pcb.with_suffix(".kicad_dru"), "track_width")
    if not rules:
        return {}
    return board_necks(pcb, bg or geom.BoardGeom.from_file(pcb), rules)


def neck_width(necks: dict, net: str, layer: str, shape) -> float | None:
    """The narrowest neck width of the net on the layer whose zone holds the
    whole shape, or None (the net's full width applies there)."""
    ws = [w for zone, w in (necks.get(net) or {}).get(layer, [])
          if zone.buffer(1e-6).covers(shape)]
    return min(ws) if ws else None


def under_width_rules(bg, widths: dict, necks: dict, uuids) -> list[dict]:
    """The tracks of `uuids` under their net's .kicad_dru width floor
    (`widths`, route_critical.dru_net_floors) that aren't wholly in one of
    its neck zones at no less than the neck width (#937, #1153):
    [{"uuid", "net", "layer", "width", "rule_mm", "track"}]."""
    uuids = set(uuids)
    out = []
    for t in bg.tracks_of():
        if not t.uuid or t.uuid not in uuids:
            continue
        floor = widths.get(t.net, 0.0)
        if t.width >= floor - 1e-4:
            continue
        nw = neck_width(necks or {}, t.net, t.layer, t.shape)
        if nw is None or t.width < nw - 1e-4:
            out.append({"uuid": t.uuid, "net": t.net, "layer": t.layer,
                        "width": round(t.width, 4), "rule_mm": floor,
                        "track": t})
    return out


def narrow_new_tracks(staged: Path, widths: dict[str, float],
                      before: set, necks: dict | None = None) -> list[str]:
    """uuids of tracks not on the board before that sit under their net's
    width rule. A track wholly in its net's neck zone at no less than the
    neck width keeps (#937): DRC's courtyard-held rule passes it."""
    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import geom
    bg = geom.BoardGeom.from_file(staged)
    new = {t.uuid for t in bg.tracks_of() if t.uuid and t.uuid not in before}
    return [r["uuid"] for r in under_width_rules(bg, widths, necks, new)]


def courtyard_neck_rule(fps: dict, neck_rules: list, row: dict):
    """The neck rule (route_critical.dru_neck_rules) of the row's net whose
    part's courtyard the track's copper meets, at a min the track's width
    meets, or None. DRC's intersectsCourtyard holds such a track to the
    neck's min, so it may pass a track the neck zone (cut to NECK_REACH_MM
    round the pads) leaves out (#1153). fps = placelib.PlaceModel's
    footprints."""
    rules = [r for r in neck_rules if r["net"] == row["net"]
             and row["width"] >= r["min_mm"] - 1e-4]
    for r in rules:
        cy = courtyard_abs(fps.get(r["ref"]))
        if cy is not None and not cy.is_empty \
                and cy.intersects(row["track"].poly):
            return r
    return None
