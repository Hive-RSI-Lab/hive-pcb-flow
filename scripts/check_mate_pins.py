"""check_mate_pins.py - do mated connectors meet net to net? (P8 verify suite)

Two failure modes, one script; the family table is
reference/connector_pinmap.yaml (see its header for keyed, mates and mounts).

MECH-05, a declared mated pair (constraints.json "mating_pairs"):
 - mate_pin_mismatch: two pads that touch when the pair is mated carry
   different nets (a mirrored pinout, swapped rows, a socket on the wrong
   side). The message says when the meeting is a supply short.
 - mate_family_mismatch: the two footprints are known families that do not
   plug into each other, or their pad patterns do not overlay.
 - mate_pair_unresolved: a declared ref, or the mate board, is not there.
 - mate_row_spacing: both are 2-row connectors at one pitch whose rows are
   a different distance apart (#1124).
 - mate_mirrored: a pair with a connector on B.Cu whose pins meet the wrong
   nets, and would all meet their own nets mirrored (the other mount): a
   bottom-side connector mirrored against its mate (#474; 5 of 18 bring-up
   post-mortems in reference/ingest/dfm-assembly.md). One error naming the
   cause, on top of the per-pin mate_pin_mismatch errors.

MECH-05, on this board alone (reference/connector_pinmap.yaml
standard_headers, a mate whose pinout is a standard):
 - mate_mirrored_pinout: a standard header (the Raspberry Pi 40-pin GPIO,
   known by the supplies on its pins) on B.Cu, facing down onto its mate
   below, whose pad pattern seen from above is the mirror of the mate's: a
   footprint numbered for F.Cu (PinHeader_2x20) flipped to the back. No
   rotation mates it; the odd and even rows swap (digits v1 J1). A connector
   in a declared pair is left to the pair rules. Each header judged is in the
   standard_headers fact (a run that judged one is not nothing_to_judge,
   #893); one whose pins 1-3 aren't the two-row pattern is named in
   not_judged.

MECH-05, a mate stated without its board (mating_pairs "mate_standard",
connector_pinmap.yaml mate_standards or a family name, #1124):
 - mate_gender_mismatch: the connector's family and the stated mate have the
   same gender (a socket where the mating side is a socket too).
 - mate_row_spacing: a 2-row connector whose rows are not the stated mate's
   row_pitch_mm apart (an edge connector footprint with 2.54 mm rows where
   the edge socket's are 5.08, zxdiag v1.0 Z101).
 - mate_family_mismatch: its pin pitch is not the mate's pitch_mm.
Each one judged is in the stated_mates fact; a connector no family knows the
gender of, or whose pads are not straight rows, is named in not_judged.

MECH-06, on this board alone:
 - mate_unkeyed_rail_swap: two connectors with the same footprint in an
   unkeyed family carry different nets on a pin where either carries a supply
   rail, so a plug made for one, put in the other, meets the wrong rail.

Cables (#485): a "cable" mount with a "cable" description resolves which pin
meets which from the cable and the sockets (lib/cablelib.py has the proof):
   "cable": {"kind": "ffc",      # ffc | fpc | idc | discrete
             "type": "B",        # ffc/fpc: A same-side, B opposite-side
             "pin_map": "straight",  # optional for ffc (then checked
                                 # against the type); idc/discrete: straight
                                 # | reversed | rows_swapped | {"1": "3"}
             "ends": {"J3": {"contact": "bottom", "entry": "tabs"}},
             "name": "W1", "length_mm": 40, "pitch_mm": 0.5, "part": "..."}
 - cable_type_unknown / cable_contact_unknown / cable_map_mismatch /
   cable_pins_mismatch / cable_map_invalid: the cable can't be resolved, or
   its declared map is not what its type gives; cable_orientation_ambiguous
   (warning): a dual-contact socket.
 - then mate_pin_mismatch per pin as for any pair; supply_on_signal marks a
   supply meeting a signal. --wireviz FILE writes the harness for WireViz.
Socket contact side and entry come from connector_pinmap.yaml ffc_sockets
or cable.ends; the same part at both ends needs neither.
Without "cable", mount "cable" is pin n to pin n, as before.

constraints.json "mating_pairs" entries:
  {"ref": "J2",                 # connector on this board
   "mate_ref": "J1",            # connector it mates with
   "mate_pcb": "mate/d.kicad_pcb",  # the mate's board, relative to the
                                # constraints file; absent = this board
   "mount": "stack",            # stack | flip | cable (default stack)
   "rotation": 0,               # mate board turn, degrees CCW, top view
   "net_map": {"+3V3": "VDD"}}  # this board's net -> the mate's name for it
  {"ref": "Z101", "mate_standard": "zx-spectrum-edge"}  # mate with no board:
                                # a mate_standards row or a family name
Nets are compared by their last path segment, case-insensitive, so "/SWDIO"
on one board meets "SWDIO" on the other. Pads with no net, or an
"unconnected-" net, on either side are not compared.

CLI: --pcb board.kicad_pcb [--constraints constraints.json] [--out report.json]
     [--wireviz harness.yaml]
exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cablelib  # noqa: E402
import checklib  # noqa: E402
import placelib  # noqa: E402
import yaml  # noqa: E402

SCRIPT = "check_mate_pins"
TABLE = (Path(__file__).resolve().parents[1] / "reference"
         / "connector_pinmap.yaml")
MOUNTS = ("stack", "flip", "cable")

GND_RE = re.compile(r"^(A|D|P|S|C)?GND\w*$|^VSS\w*$|^0V$", re.I)
RAIL_RE = re.compile(r"^[+-]\d|^\+|^(VBUS|VCC|VDD|VIN|VSYS|VBAT|VM|VDDA|VREF)"
                     r"(\b|_|\d|$)|^V\d", re.I)


def load_table(path=TABLE) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    fams = []
    for f in doc.get("families") or []:
        fams.append({**f, "re": re.compile(f["match"], re.I)})
    stds = []
    for h in doc.get("standard_headers") or []:
        stds.append({**h, "net_re": {k: re.compile(v, re.I)
                                     for k, v in h["nets"].items()},
                     "pin_class": {str(n): k for k, ns in
                                   h["supply_pins"].items() for n in ns}})
    socks = [{**f, "re": re.compile(f["match"], re.I)}
             for f in doc.get("ffc_sockets") or []]
    return {"tol": float((doc.get("defaults") or {}).get("overlay_tol_mm", 0.2)),
            "families": fams, "standards": stds, "ffc_sockets": socks,
            "mate_standards": list(doc.get("mate_standards") or [])}


def family(fpid: str, table: dict) -> dict | None:
    name = fpid.split(":", 1)[-1]
    for f in table["families"]:
        if f["re"].search(name):
            return f
    return None


def canon(net: str | None) -> str | None:
    """Comparable net name, or None for a pad that carries nothing."""
    if not net or net.lower().startswith("unconnected-"):
        return None
    return net.rstrip("/").rsplit("/", 1)[-1].upper()


def is_gnd(net: str | None) -> bool:
    name = canon(net)
    return bool(name) and bool(GND_RE.match(name))


def is_rail(net: str | None) -> bool:
    """A supply rail other than ground ("/VCC" and "VCC" alike)."""
    name = canon(net)
    return bool(name) and not is_gnd(name) and bool(RAIL_RE.match(name))


def is_supply(net: str | None) -> bool:
    return is_gnd(net) or is_rail(net)


def _pads(fp) -> list[tuple[str, str | None, float, float]]:
    """(number, net, x, y) of the connector's numbered pads, in top view."""
    return [p for p in fp.pad_centers_abs() if p[0]]


def _offsets(pads, mount="stack", rotation=0.0):
    """Pad offsets from the pattern centre, mate frame turned into ours."""
    cx = sum(p[2] for p in pads) / len(pads)
    cy = sum(p[3] for p in pads) / len(pads)
    a = math.radians(rotation)
    out = []
    for num, net, x, y in pads:
        dx, dy = x - cx, y - cy
        if mount == "flip":
            dx = -dx
        # CCW in top view with KiCad's y pointing down
        rx = dx * math.cos(a) + dy * math.sin(a)
        ry = -dx * math.sin(a) + dy * math.cos(a)
        out.append((num, net, rx, ry))
    return out


def meetings(a_fp, b_fp, mount, rotation, tol):
    """[(a_pad, b_pad)] that touch when mated, or None when the patterns do
    not line up (different count, pitch or rows)."""
    a_pads, b_pads = _pads(a_fp), _pads(b_fp)
    if not a_pads or len(a_pads) != len(b_pads):
        return None
    if mount == "cable":
        by_num = {p[0]: p for p in b_pads}
        if {p[0] for p in a_pads} != set(by_num):
            return None
        return [(p, by_num[p[0]]) for p in a_pads]
    a_off = _offsets(a_pads)
    b_off = _offsets(b_pads, mount, rotation)
    pairs = []
    for i, (_, _, ax, ay) in enumerate(a_off):
        hit = [j for j, (_, _, bx, by) in enumerate(b_off)
               if math.hypot(ax - bx, ay - by) <= tol]
        if len(hit) != 1:
            return None
        pairs.append((a_pads[i], b_pads[hit[0]]))
    return pairs


def _clusters(vals, tol):
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def row_geometry(fp, tol: float = 0.1) -> dict | None:
    """{rows, row_pitch, pitch} of a connector's numbered pads in its own
    frame: 1 or 2 straight rows (row_pitch None for one). None for any other
    pattern, or fewer than 2 pads."""
    pts = {(round(p.local[0], 3), round(p.local[1], 3))
           for p in fp.pads if p.number}
    if len(pts) < 2:
        return None
    xs = _clusters([x for x, _ in pts], tol)
    ys = _clusters([y for _, y in pts], tol)
    for across, along in ((xs, ys), (ys, xs)):
        if len(across) <= 2 and len(along) >= 2 and \
                len(across) * len(along) >= len(pts):
            steps = sorted(b - a for a, b in zip(along, along[1:]))
            return {"rows": len(across),
                    "row_pitch": (round(across[1] - across[0], 3)
                                  if len(across) == 2 else None),
                    "pitch": round(steps[0], 3)}
    return None


def stated_mate(name: str, table: dict) -> dict | None:
    """The mate_standards row, or the family, a pair's mate_standard names."""
    for row in table["mate_standards"]:
        if row["name"] == name:
            return {**row, "family": False}
    for f in table["families"]:
        if f["name"] == name:
            return {**f, "family": True,
                    "pitch_mm": f.get("row_pitch_mm"),
                    "row_pitch_mm": None}
    return None


def stated_violations(model, pairs: list, table: dict
                      ) -> tuple[list[dict], list[dict], list[str]]:
    """MECH-05 against a mate named by mate_standard (no mate board, #1124):
    gender, pin pitch and row spacing. (violations, facts, not judged)."""
    out, facts, left = [], [], []
    tol = table["tol"]
    for pair in pairs:
        ref, name = pair["ref"], pair["mate_standard"]
        std = stated_mate(name, table)
        if std is None:
            raise checklib.CheckError(
                f"mating_pairs {ref}: mate_standard {name!r} is not a "
                "mate_standards row or family in connector_pinmap.yaml")
        fp = model.footprints.get(ref)
        if fp is None:
            out.append(checklib.violation(
                "mate_pins", "error", None, None, None, [ref],
                f"declared mate {ref} -> {name}: {ref} is not on this board",
                SCRIPT, kind="mate_pair_unresolved", connector=ref,
                mate=name))
            continue
        fam = family(fp.fpid, table)
        geo = row_geometry(fp)
        short = fp.fpid.split(":")[-1]
        where = fp.center_abs()
        fact = {"ref": ref, "mate_standard": name,
                "family": fam["name"] if fam else None,
                "geometry": geo, "judged": []}
        g_a, g_b = (fam or {}).get("gender"), std.get("gender")
        if g_a and g_b:
            fact["judged"].append("gender")
            if g_a == g_b and name not in (fam.get("mates") or []):
                out.append(checklib.violation(
                    "mate_pins", "error", where, None, None, [ref],
                    f"{ref} ({short}) is {g_a}, and so is the {name} it is "
                    f"stated to mate with: a {g_a} part cannot take a "
                    f"{g_b} one. Use the other gender",
                    SCRIPT, kind="mate_gender_mismatch", connector=ref,
                    mate=name, gender=g_a, mate_gender=g_b))
        elif not g_a:
            left.append(f"{ref} ({short}): no connector family gives its "
                        f"gender, so it was not judged against {name}")
        if geo is None:
            if std.get("pitch_mm") or std.get("row_pitch_mm"):
                left.append(f"{ref} ({short}): its pads are not one or two "
                            f"straight rows, so pitch and row spacing were "
                            f"not judged against {name}")
        else:
            pitch = std.get("pitch_mm")
            if pitch:
                fact["judged"].append("pitch")
                if abs(geo["pitch"] - float(pitch)) > tol:
                    out.append(checklib.violation(
                        "mate_pins", "error", where, None, None, [ref],
                        f"{ref} ({short}) has its pins {geo['pitch']:g} mm "
                        f"apart, and {name} {float(pitch):g} mm",
                        SCRIPT, kind="mate_family_mismatch", connector=ref,
                        mate=name, pitch_mm=geo["pitch"],
                        mate_pitch_mm=float(pitch)))
            want = std.get("row_pitch_mm")
            want = [float(w) for w in (want if isinstance(want, list)
                                       else [want] if want else [])]
            if want and geo["rows"] == 2:
                fact["judged"].append("row_spacing")
                if all(abs(geo["row_pitch"] - w) > tol for w in want):
                    ok = " or ".join(f"{w:g}" for w in want)
                    src = f" ({std['source']})" if std.get("source") else ""
                    out.append(checklib.violation(
                        "mate_pins", "error", where, None, None, [ref],
                        f"{ref} ({short}) has its two rows "
                        f"{geo['row_pitch']:g} mm apart; {name} needs {ok} mm"
                        f"{src}, so it cannot go in",
                        SCRIPT, kind="mate_row_spacing", connector=ref,
                        mate=name, row_pitch_mm=geo["row_pitch"],
                        mate_row_pitch_mm=want))
        facts.append(fact)
    return out, facts, left


def _resolve_mate(pair: dict, pcb: Path, cons_dir: Path | None, model,
                  cache: dict):
    rel = pair.get("mate_pcb")
    if not rel:
        return model, pcb
    path = Path(rel)
    if not path.is_absolute():
        path = (cons_dir or pcb.parent) / path
    if path not in cache:
        cache[path] = placelib.PlaceModel(path) if path.is_file() else None
    return cache[path], path


def pair_violations(model, pcb: Path, pairs: list, cons_dir: Path | None,
                    table: dict, cables: list | None = None
                    ) -> tuple[list[dict], list[dict]]:
    out, facts, cache = [], [], {}
    if cables is None:
        cables = []
    for pair in pairs:
        ref, mref = pair.get("ref"), pair.get("mate_ref")
        mount = pair.get("mount", "stack")
        if mount not in MOUNTS:
            raise checklib.CheckError(
                f"mating_pairs {ref}: mount {mount!r} not in {MOUNTS}")
        a_fp = model.footprints.get(ref)
        mate, mpath = _resolve_mate(pair, pcb, cons_dir, model, cache)
        b_fp = mate.footprints.get(mref) if mate is not None else None
        if a_fp is None or b_fp is None:
            what = (f"{ref} is not on this board" if a_fp is None else
                    f"mate board {mpath.name} not found" if mate is None else
                    f"{mref} is not on {mpath.name}")
            out.append(checklib.violation(
                "mate_pins", "error",
                a_fp.center_abs() if a_fp is not None else None, None, None,
                [ref] if ref else [],
                f"declared mated pair {ref} <-> {mref}: {what}", SCRIPT,
                kind="mate_pair_unresolved", connector=ref, mate=mref))
            continue
        cable = pair.get("cable")
        if mount == "cable" and isinstance(cable, dict):
            try:
                res = cablelib.resolve(a_fp, b_fp, cable, table["ffc_sockets"])
            except cablelib.CableError as e:
                out.append(checklib.violation(
                    "mate_pins", "warning" if e.kind ==
                    "cable_orientation_ambiguous" else "error",
                    a_fp.center_abs(), None, None, [ref],
                    f"cable {ref} <-> {mref} on {mpath.name}: {e}", SCRIPT,
                    kind=e.kind, connector=ref, mate=mref))
                continue
            b_by = {p[0]: p for p in _pads(b_fp)}
            a_by = {p[0]: p for p in _pads(a_fp)}
            met = [(a_by[k], b_by[v]) for k, v in
                   sorted(res["map"].items(), key=lambda i: int(i[0])
                          if i[0].isdigit() else 0)]
            fam_bad = False
            cables.append({"name": cable.get("name"), "a_board": pcb.stem,
                           "a_fp": a_fp, "b_board": mpath.stem, "b_fp": b_fp,
                           "cable": cable, "resolved": res})
        else:
            res = None
            fa, fb = family(a_fp.fpid, table), family(b_fp.fpid, table)
            met = meetings(a_fp, b_fp, mount, float(pair.get("rotation", 0)),
                           table["tol"])
            fam_bad = fa and fb and fb["name"] not in (fa.get("mates") or [])
        if met is None and not fam_bad and mount != "cable":
            ga, gb = row_geometry(a_fp), row_geometry(b_fp)
            if ga and gb and ga["rows"] == gb["rows"] == 2 \
                    and abs(ga["pitch"] - gb["pitch"]) <= table["tol"] \
                    and abs(ga["row_pitch"] - gb["row_pitch"]) > table["tol"]:
                out.append(checklib.violation(
                    "mate_pins", "error", a_fp.center_abs(), None, None,
                    [ref],
                    f"{ref} ({a_fp.fpid.split(':')[-1]}) has its two rows "
                    f"{ga['row_pitch']:g} mm apart and {mref} "
                    f"({b_fp.fpid.split(':')[-1]}) on {mpath.name} "
                    f"{gb['row_pitch']:g} mm: they cannot mate",
                    SCRIPT, kind="mate_row_spacing", connector=ref,
                    mate=mref, row_pitch_mm=ga["row_pitch"],
                    mate_row_pitch_mm=[gb["row_pitch"]]))
                continue
        if met is None or fam_bad:
            why = (f"{fa['name']} does not plug into {fb['name']}" if fam_bad
                   else f"pad patterns do not line up ({mount} mount)")
            out.append(checklib.violation(
                "mate_pins", "error", a_fp.center_abs(), None, None, [ref],
                f"{ref} ({a_fp.fpid.split(':')[-1]}) cannot mate with "
                f"{mref} ({b_fp.fpid.split(':')[-1]}) on {mpath.name}: {why}",
                SCRIPT, kind="mate_family_mismatch", connector=ref, mate=mref))
            continue
        nmap = {canon(k): canon(v) for k, v in
                (pair.get("net_map") or {}).items()}
        rot = float(pair.get("rotation", 0))
        bad = _mismatches(met, nmap)
        back = [r for r, fp in ((ref, a_fp), (mref, b_fp))
                if fp.side == "back"]
        alt = {"stack": "flip", "flip": "stack"}.get(mount)
        alt_met = (meetings(a_fp, b_fp, alt, rot, table["tol"])
                   if bad and back and alt else None)
        if alt_met is not None and _mismatches(alt_met, nmap) == 0 \
                and _compared(alt_met, nmap) >= 2:
            p1 = next(((a, b) for a, b in met if a[0] == "1"), met[0])
            out.append(checklib.violation(
                "mate_pins", "error", a_fp.center_abs(), None, None, [ref],
                f"{ref} <-> {mref} on {mpath.name}: {' and '.join(back)} on "
                f"the bottom (B.Cu) is mirrored against its mate - pin "
                f"{p1[0][0]} meets pin {p1[1][0]}, and {bad} of "
                f"{_compared(met, nmap)} pins meet the wrong net; turned the "
                f"other way ({alt} mount) every pin would meet its own. Use "
                f"a footprint numbered for the back side (or mirror the "
                f"pin-to-net map), or put the connector on the other side",
                SCRIPT, kind="mate_mirrored", connector=ref, mate=mref,
                bottom=back, mismatched=bad))
        for (anum, anet, ax, ay), (bnum, bnet, _, _) in met:
            ca, cb = canon(anet), canon(bnet)
            if ca is None or cb is None or nmap.get(ca, ca) == cb:
                continue
            short = (is_supply(anet) and is_supply(bnet))
            on_sig = not short and (is_supply(anet) or is_supply(bnet))
            via = (f" through a {res['name']} cable" if res else "")
            out.append(checklib.violation(
                "mate_pins", "error", (ax, ay), None, anet, [ref],
                f"{ref} pin {anum} ({anet}) meets {mref} pin {bnum} ({bnet}) "
                f"on {mpath.name}{via}" + (
                    ": a supply short when mated" if short else
                    ": a supply tied to a signal" if on_sig else ""),
                SCRIPT, kind="mate_pin_mismatch", connector=ref, mate=mref,
                pin=anum, mate_pin=bnum, mate_net=bnet, supply_short=short,
                supply_on_signal=on_sig))
        fact = {"ref": ref, "mate_ref": mref, "mate_pcb": mpath.name,
                "mount": mount, "pins": len(met), "mismatched": bad,
                "compared": _compared(met, nmap)}
        if res:
            fact["cable"] = {k: res[k] for k in ("name", "type", "note", "pins")}
        facts.append(fact)
    return out, facts


def _compared(met, nmap) -> int:
    return sum(1 for a, b in met
               if canon(a[1]) is not None and canon(b[1]) is not None)


def _mismatches(met, nmap) -> int:
    return sum(1 for a, b in met
               if canon(a[1]) is not None and canon(b[1]) is not None
               and nmap.get(canon(a[1]), canon(a[1])) != canon(b[1]))


def handedness(pads, pitch: float, tol: float) -> int | None:
    """+1/-1: the sign of (pin 2 - pin 1) x (pin 3 - pin 1) in top view (y
    down), for a 2-row odd/even pattern; None when pins 1-3 are not one
    pitch apart at a right angle (not that pattern)."""
    at = {p[0]: (p[2], p[3]) for p in pads}
    if not all(n in at for n in ("1", "2", "3")):
        return None
    ax, ay = at["2"][0] - at["1"][0], at["2"][1] - at["1"][1]
    bx, by = at["3"][0] - at["1"][0], at["3"][1] - at["1"][1]
    if abs(math.hypot(ax, ay) - pitch) > tol \
            or abs(math.hypot(bx, by) - pitch) > tol \
            or abs(ax * bx + ay * by) > tol * pitch:
        return None
    return 1 if ax * by - ay * bx > 0 else -1


def standard_match(pads, std: dict) -> dict | None:
    """{pin: supply} of the pads that make this connector the standard
    header, or None when it is not (too few, or a supply on a wrong pin)."""
    if len(pads) != std["pins"]:
        return None
    hits = {}
    for num, net, _, _ in pads:
        name = canon(net)
        if name is None:
            continue
        cls = next((k for k, r in std["net_re"].items() if r.match(name)),
                   None)
        if cls is None:
            continue
        if std["pin_class"].get(num) != cls:
            return None
        hits[num] = cls
    if len(hits) < 2 or set(hits.values()) <= {"GND"}:
        return None
    return hits


def standard_violations(model, table: dict, declared: set,
                        judged: list | None = None,
                        left: list | None = None) -> list[dict]:
    """Back-side headers that match a standard and face down mirrored.

    judged gets each header whose handedness was read (pass or fail), so
    a run that checked one is not nothing_to_judge (#893); left gets each
    match whose handedness couldn't be read."""
    out = []
    for fp in sorted(model.footprints.values(), key=lambda f: f.ref):
        if fp.side != "back" or fp.ref in declared:
            continue
        pads = _pads(fp)
        for std in table["standards"]:
            hits = standard_match(pads, std)
            if hits is None:
                continue
            h = handedness(pads, float(std["pitch_mm"]), table["tol"])
            if h is None:
                if left is not None:
                    left.append(
                        f"{fp.ref} ({fp.fpid.split(':')[-1]}) on B.Cu matches "
                        f"a {std['title']}'s supply pins but its pins 1-3 "
                        "aren't its two-row pattern, so whether it is "
                        "mirrored was not judged")
                break
            if judged is not None:
                judged.append(fp.ref)
            if h == std["face_up_handedness"]:
                break
            # rows swap: pin 2k-1 meets the mate's pin 2k and back
            nets = {p[0]: p[1] for p in pads}
            meets = []
            for num in sorted(hits, key=int):
                other = str(int(num) + 1 if int(num) % 2 else int(num) - 1)
                theirs = std["pin_class"].get(other, "a signal")
                meets.append(f"pin {num} ({nets[num]}) meets the mate's "
                             f"pin {other} ({theirs})")
            p1 = next(p for p in pads if p[0] == "1")
            out.append(checklib.violation(
                "mate_pins", "error", (p1[2], p1[3]), None, p1[1], [fp.ref],
                f"{fp.ref} ({fp.fpid.split(':')[-1]}) on B.Cu is a "
                f"{std['title']} facing down onto its mate below, but seen "
                "from above its pads are the mirror of the mate's header: "
                "the odd and even rows swap whichever way it is turned ("
                + "; ".join(meets) + "). Use a footprint numbered for the "
                "back (the socket's own numbering, e.g. PinSocket on B.Cu) "
                "or put it on F.Cu", SCRIPT, kind="mate_mirrored_pinout",
                connector=fp.ref, mate=std["name"],
                supply_pins=dict(sorted(hits.items(), key=lambda i: int(i[0])))))
            break
    return out


def unkeyed_violations(model, table: dict) -> list[dict]:
    groups: dict[str, list] = {}
    for fp in sorted(model.footprints.values(), key=lambda f: f.ref):
        fam = family(fp.fpid, table)
        if fam is not None and not fam.get("keyed", True):
            groups.setdefault(fp.fpid, []).append(fp)
    out = []
    for fps in groups.values():
        for i, a in enumerate(fps):
            a_nets = {p[0]: p for p in _pads(a)}
            for b in fps[i + 1:]:
                for num, bnet, _, _ in _pads(b):
                    if num not in a_nets:
                        continue
                    _, anet, ax, ay = a_nets[num]
                    if canon(anet) == canon(bnet) or not (
                            is_rail(anet) or is_rail(bnet)):
                        continue
                    out.append(checklib.violation(
                        "mate_pins", "error", (ax, ay), None, anet,
                        [a.ref, b.ref],
                        f"{a.ref} and {b.ref} are the same unkeyed connector "
                        f"but pin {num} carries {anet or 'nothing'} on "
                        f"{a.ref} and {bnet or 'nothing'} on {b.ref}: a plug "
                        "in the wrong one meets the wrong rail", SCRIPT,
                        kind="mate_unkeyed_rail_swap", connector=a.ref,
                        mate=b.ref, pin=num, mate_net=bnet))
    return out


CONNECTOR_REF_RE = re.compile(r"^(J|P|CN|CON|X)\d")


def identical_connectors(model, table: dict) -> tuple[int, list[str]]:
    """(identical unkeyed connector groups the rail-swap rule judges, the
    same-footprint connectors it can't: no family says if they are keyed)."""
    groups: dict[str, list] = {}
    for fp in sorted(model.footprints.values(), key=lambda f: f.ref):
        if CONNECTOR_REF_RE.match(fp.ref):
            groups.setdefault(fp.fpid, []).append(fp.ref)
    judged, left = 0, []
    for fpid, refs in groups.items():
        if len(refs) < 2:
            continue
        fam = family(fpid, table)
        if fam is None:
            left.append(f"{', '.join(refs)} ({fpid.split(':')[-1]}): no "
                        "connector family knows this footprint, so a plug in "
                        "the wrong one was not judged")
        elif not fam.get("keyed", True):
            judged += 1
    return judged, left


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--constraints", default=None,
                    help="constraints.json (mating_pairs list); optional")
    ap.add_argument("--out", default=None)
    ap.add_argument("--wireviz", default=None,
                    help="write the declared cables as a WireViz harness YAML")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    cons = checklib.load_json(args.constraints, "constraints") \
        if args.constraints else {}
    pairs = cons.get("mating_pairs") or []
    if not isinstance(pairs, list) or not all(
            isinstance(p, dict) and p.get("ref")
            and bool(p.get("mate_ref")) != bool(p.get("mate_standard"))
            for p in pairs):
        raise checklib.CheckError(
            "constraints mating_pairs must be a list of {ref, mate_ref, ...} "
            "or {ref, mate_standard}")
    stated = [p for p in pairs if p.get("mate_standard")]
    pairs = [p for p in pairs if p.get("mate_ref")]
    table = load_table()
    model = placelib.PlaceModel(pcb)
    cons_dir = Path(args.constraints).parent if args.constraints else None
    cables: list = []
    violations, facts = pair_violations(model, pcb, pairs, cons_dir, table,
                                        cables)
    found, stated_facts, stated_left = stated_violations(model, stated, table)
    violations += found
    if args.wireviz:
        Path(args.wireviz).write_text(
            yaml.safe_dump(cablelib.wireviz(cables), sort_keys=False),
            encoding="utf-8")
    judged: list = []
    unread: list = []
    violations += standard_violations(
        model, table, {p.get("ref") for p in pairs + stated}, judged, unread)
    violations += unkeyed_violations(model, table)
    for v in violations:
        v["check"] = "mate_pins"
    groups, left = identical_connectors(model, table)
    left += [f"{f['ref']} <-> {f['mate_ref']}: no pin has a net on both sides"
             for f in facts if not f["compared"]] + unread + stated_left
    compared = any(f["compared"] for f in facts) or any(
        f["judged"] for f in stated_facts)
    return checklib.report(
        SCRIPT, str(pcb), violations, pairs=facts, stated_mates=stated_facts,
        standard_headers=judged, not_judged=left,
        nothing_to_judge=None if compared or groups or judged else
        "no mating_pairs compared, no identical unkeyed connectors and no "
        "standard header on B.Cu"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
