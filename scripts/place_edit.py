"""place_edit - apply an atomic footprint move/rotate/flip/lock op list (S9, SPEC P6.3).

The pipeline's ONLY writer for placement changes (SPEC section 4: op lists are
applied atomically; agents never raw-edit the board). Edit path decision (V4/V8,
recorded in PROGRESS S9): SWIG bundled python, headless - the spec's "IPC
headless" does not exist at the KiCad-10 pin (no api-server; kipy needs a GUI
pcbnew and the sandboxed-GUI probe now fails "KiCad is not ready to reply").
kipy/IPC is the KiCad-11 migration target.

Contract:
  place_edit.py --pcb B.kicad_pcb --ops ops.json [--out-report r.json]
  ops.json: {"version": 1, "ops": [...]}  (op shapes: see lib/place_swig.py)

flip, when it changes the side, mirrors the part top to bottom about its own
position, not left to right: each pad's local y and the part's angle are
negated (-90 becomes 90), local x is kept. Follow it with rotate (or use
place with deg) to set the angle you want.

Atomic + rollback: ops are validated first (schema + refs against the board);
the board is copied to a scratch dir INSIDE the board's directory; the SWIG
worker edits and saves the copy; the driver re-parses the copy and verifies
every op's target landed (position 1e-3 mm, angle 0.05 deg, side, locked);
only then does os.replace() swap it in (same volume -> atomic). Any failure
leaves the original board byte-identical. Re-applying the same op list is
idempotent (ops are absolute). exit 0 applied+verified / 2 error (rolled back).

set_pad re-nets ("" clears), resizes and/or sets the zone connection
(solid|thermal|none|inherit) of a footprint's pads; set_dnp sets
its do-not-populate flag. --op JSON (repeatable) takes ops inline instead of
--ops FILE. --refill refills zones on the staged copy before the swap (with
the board's .kicad_pro/.kicad_dru beside it), as route_edit --refill does.

set_text (hide / resize / re-layer a Reference or Value field) is the fix
for a refdes with no legal spot near its own pads (dense 0603 rows): on a
silk layer it refuses a size below check_silk's legibility floor or a stroke
below the fab's silk width floor (silk_text_floor()).

swap_fp puts a .kicad_mod FILE (a fixed library footprint) in place of a
placed part's footprint, on a placed or routed board, as KiCad's "update
footprint from library" does: the part keeps its reference, value, fields,
schematic link, field texts, DNP and lock; each pad takes the net the old
pad of its number had (`nets` {number: net} overrides, "" = no net) and,
where the board set one on every old pad of that number, its zone
connection. renumber gives the pads found at board positions new numbers
(`pads` [{"at": [x, y], "number": N}], 0.005 mm), each with the net of
number N (`nets` again overrides) - the lighter fix when only the numbering
was wrong and no graphic follows pin 1. Both refuse when a track or via of
another net touches one of the pads they produce (KiCad would carry that
copper onto the pad's net when it saves): rip it first with route_edit,
re-route after; part_repair.py does the three steps and checks them.

Note: KiCad regenerates footprint/graphic UUIDs on every save, so two saves of
the same placement are NOT byte-identical - compare parsed positions, never
file hashes.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import env  # noqa: E402
import placelib  # noqa: E402
from checklib import CheckError  # noqa: E402

WORKER = SCRIPTS / "lib" / "place_swig.py"

OP_FIELDS = {  # op -> (required, optional)
    "place": ({"ref", "x", "y"}, {"deg", "side"}),
    "move": ({"ref", "x", "y"}, set()),
    "rotate": ({"ref", "deg"}, set()),
    "flip": ({"ref", "side"}, set()),
    "lock": ({"ref", "locked"}, set()),
    # S14 text ops (V17): board-frame silk/fab text + refdes/value moves
    "add_text": ({"text", "x", "y", "layer"}, {"deg", "size", "thickness"}),
    # remove_text + add_text is the only scripted way to RELOCATE a gr_text
    # (add_text matches on the TARGET position, so it can never move one).
    "remove_text": ({"text", "x", "y", "layer"}, set()),
    "move_text": ({"ref", "field", "x", "y"}, {"deg"}),
    # hide / resize / re-layer a Reference|Value field in place: the fix when
    # no legal spot for a refdes exists near its own pads (dense 0603 rows),
    # where move_text has nowhere to go. Needs at least one of the options.
    "set_text": ({"ref", "field"}, {"hide", "size", "thickness", "layer"}),
    # footprint-INTERNAL silk graphics on an already-placed board: a library
    # edit cannot reach one without re-running board_init (and losing the
    # placement), so this is the only scripted route. Never touches text.
    "silk_clear": ({"ref"}, {"layer", "only_offboard", "region",
                             "over_pads", "edge_mm", "at", "uuid"}),
    # pad and part-attribute ECOs (the hi-fi boards' pad_nets / pad_nonet /
    # pad_size / set_dnp scripts): re-net or clear a pad (an NC pin, a
    # rewire board_update refuses because copper touches the pad - rip it
    # with route_edit first), resize pads (an annular-ring DFM fix), mark a
    # part do-not-populate
    # zone_connection: solid|thermal|none|inherit per pad (the boards'
    # pad_zone_full / j1_shell_solid scripts: a USB-C power pin or a
    # connector shell joined to its plane without thermal spokes)
    "set_pad": ({"ref", "pad"}, {"net", "size", "zone_connection"}),
    "set_dnp": ({"ref", "dnp"}, set()),
    # re-pad a placed part (part_repair's ops): swap_fp loads a .kicad_mod
    # file in place of its footprint (x/y/deg/side: where it goes, default
    # where the old one sat; fpid: "Lib:Name" the board records); renumber
    # gives the pads at board positions new numbers
    "swap_fp": ({"ref", "footprint"}, {"fpid", "nets", "x", "y", "deg",
                                       "side"}),
    "renumber": ({"ref", "pads"}, {"nets"}),
}
TEXT_LAYERS = {"F.SilkS", "B.SilkS", "F.Fab", "B.Fab"}
POS_TOL = 1e-3   # mm
ANG_TOL = 0.05   # deg
REPAD_TOL = 0.005  # mm: a renumber `at` finds its pad within this
REPAD_OPS = ("swap_fp", "renumber")
# copper-relevant ops (refused on a routed board without --allow-routed)
FOOTPRINT_OPS = {"place", "move", "rotate", "flip", *REPAD_OPS}
# ops that may not share an op list with a swap_fp/renumber of the same part
# (a re-pad sets the part's pose and its pads' nets itself)
REPAD_EXCLUSIVE = ("place", "move", "rotate", "flip", "set_pad")
TEXT_OPS = ("add_text", "remove_text", "move_text", "set_text")
SILK_LAYERS = {"F.SilkS", "B.SilkS"}
SIZE_TOL = 1e-3  # mm
# set_pad zone_connection -> the pad's saved (zone_connect N), None = absent
# (inherit). Same numbers as KiCad 10's ZONE_CONNECTION enum.
ZONE_CONNECT = {"inherit": None, "none": 0, "thermal": 1, "solid": 2}


def silk_text_floor() -> tuple[float, float]:
    """(min size, min stroke) for refdes/value text that stays on silk.

    Size is check_silk's legibility floor (MIN_TEXT_H, measured on size_y).
    Stroke is the larger of check_silk's MIN_TEXT_TH and the fab's silk width
    floor (jlc_capabilities.yaml min_silk_width_mm, the same number
    rules_gen writes as the .kicad_dru aiee_silk_width_floor text_thickness
    rule), taken across every profile because the op does not know the
    board's. A shrink below either would trade one finding for another."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import check_silk
    import yaml
    caps = yaml.safe_load((SCRIPTS.parent / "reference"
                           / "jlc_capabilities.yaml").read_text(
                               encoding="utf-8"))
    widths = [row["min_silk_width_mm"]
              for row in (caps.get("design_rules") or {}).values()
              if isinstance(row, dict) and "min_silk_width_mm" in row]
    return (check_silk.MIN_TEXT_H,
            max([check_silk.MIN_TEXT_TH] + widths))


def board_routed(pcb: Path) -> bool:
    """Cheap scan: does the board already carry routed copper?

    Matches top-level (segment ...) / (via ...) nodes; the trailing \\s keeps
    rule-area '(vias not_allowed)' tokens from false-positiving."""
    text = Path(pcb).read_text(encoding="utf-8")
    return re.search(r"\((?:segment|via)\s", text) is not None


def validate_ops(doc: dict) -> list[dict]:
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise CheckError("ops file must be {'version': 1, 'ops': [...]}")
    ops = doc.get("ops")
    if not isinstance(ops, list) or not ops:
        raise CheckError("ops list is empty")
    for i, op in enumerate(ops):
        kind = op.get("op")
        if kind not in OP_FIELDS:
            raise CheckError(f"ops[{i}]: unknown op '{kind}'")
        req, opt = OP_FIELDS[kind]
        missing = req - op.keys()
        if missing:
            raise CheckError(f"ops[{i}] ({kind}): missing {sorted(missing)}")
        unknown = op.keys() - req - opt - {"op"}
        if unknown:
            raise CheckError(f"ops[{i}] ({kind}): unknown keys {sorted(unknown)}")
        for k in ("x", "y", "deg"):
            if k in op and not (isinstance(op[k], (int, float))
                                and math.isfinite(op[k])):
                raise CheckError(f"ops[{i}]: {k} must be a finite number")
        if "side" in op and op["side"] not in ("front", "back"):
            raise CheckError(f"ops[{i}]: side must be front|back")
        if "locked" in op and not isinstance(op["locked"], bool):
            raise CheckError(f"ops[{i}]: locked must be a boolean")
        if kind in ("add_text", "remove_text"):
            if op["layer"] not in TEXT_LAYERS:
                raise CheckError(f"ops[{i}]: layer must be one of "
                                 f"{sorted(TEXT_LAYERS)}")
            if not isinstance(op["text"], str) or not op["text"].strip():
                raise CheckError(f"ops[{i}]: text must be a non-empty string")
            for k in ("size", "thickness"):
                if k in op and not (isinstance(op[k], (int, float))
                                    and 0 < op[k] < 20):
                    raise CheckError(f"ops[{i}]: {k} out of range")
        if kind in ("move_text", "set_text") \
                and op["field"] not in ("reference", "value"):
            raise CheckError(f"ops[{i}]: field must be reference|value")
        if kind == "set_text":
            _validate_set_text(i, op)
        if kind == "set_pad":
            _validate_set_pad(i, op)
        if kind in REPAD_OPS:
            _validate_repad(i, op)
        if kind == "set_dnp" and not isinstance(op["dnp"], bool):
            raise CheckError(f"ops[{i}]: dnp must be a boolean")
        if kind == "silk_clear":
            if op.get("layer", "F.SilkS") not in ("F.SilkS", "B.SilkS"):
                raise CheckError(f"ops[{i}]: layer must be F.SilkS|B.SilkS")
            for flag in ("only_offboard", "over_pads"):
                if flag in op and not isinstance(op[flag], bool):
                    raise CheckError(f"ops[{i}]: {flag} must be a boolean")
            e = op.get("edge_mm")
            if e is not None and (isinstance(e, bool)
                                  or not isinstance(e, (int, float)) or e < 0):
                raise CheckError(f"ops[{i}]: edge_mm must be a number >= 0")
            a = op.get("at")
            if a is not None and not (
                    isinstance(a, list) and len(a) == 2
                    and all(isinstance(x, (int, float))
                            and not isinstance(x, bool) for x in a)):
                raise CheckError(f"ops[{i}]: at must be [x, y] mm")
            u = op.get("uuid")
            if u is not None and not (
                    isinstance(u, list) and u
                    and all(isinstance(x, str) and x for x in u)):
                raise CheckError(f"ops[{i}]: uuid must be a list of uuids")
            r = op.get("region")
            if r is not None and not (
                    isinstance(r, list) and len(r) == 4
                    and all(isinstance(x, (int, float)) for x in r)
                    and r[0] < r[2] and r[1] < r[3]):
                raise CheckError(f"ops[{i}]: region must be [x0, y0, x1, y1]"
                                 " mm with x0 < x1, y0 < y1")
    _validate_repad_list(ops)
    return ops


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def _validate_repad(i: int, op: dict) -> None:
    kind = op["op"]
    nets = op.get("nets")
    if nets is not None and not (
            isinstance(nets, dict)
            and all(isinstance(k, str) and k and isinstance(v, str)
                    for k, v in nets.items())):
        raise CheckError(f"ops[{i}] ({kind}): nets must be {{pad number: "
                         "net name}} (\"\" = no net)")
    if kind == "swap_fp":
        fp = op["footprint"]
        if not (isinstance(fp, str) and fp.endswith(".kicad_mod")
                and len(fp) > len(".kicad_mod")):
            raise CheckError(f"ops[{i}] (swap_fp): footprint must be the "
                             "path of a .kicad_mod file")
        fpid = op.get("fpid")
        if fpid is not None and not (
                isinstance(fpid, str) and fpid.count(":") == 1
                and all(fpid.split(":"))):
            raise CheckError(f"ops[{i}] (swap_fp): fpid must be "
                             "\"Library:Name\"")
        if ("x" in op) != ("y" in op):
            raise CheckError(f"ops[{i}] (swap_fp): give x and y together")
        return
    pads = op["pads"]
    if not (isinstance(pads, list) and pads and all(
            isinstance(p, dict) and set(p) == {"at", "number"}
            and isinstance(p["number"], str) and p["number"]
            and isinstance(p["at"], list) and len(p["at"]) == 2
            and all(_is_num(v) for v in p["at"]) for p in pads)):
        raise CheckError(f"ops[{i}] (renumber): pads must be a list of "
                         "{\"at\": [x, y] mm, \"number\": N}")
    for a in range(len(pads)):
        for b in range(a):
            if math.dist(pads[a]["at"], pads[b]["at"]) <= REPAD_TOL:
                raise CheckError(f"ops[{i}] (renumber): pads[{b}] and "
                                 f"pads[{a}] name the same pad")


def _validate_repad_list(ops: list[dict]) -> None:
    """One re-pad per part, alone with its pose and pad nets: a swap_fp or
    renumber sets those itself (from the board as it was before the list)."""
    first: dict[str, int] = {}
    for i, op in enumerate(ops):
        if op["op"] not in REPAD_OPS:
            continue
        if op["ref"] in first:
            raise CheckError(f"ops[{i}] ({op['op']}): {op['ref']} is "
                             f"already re-padded by ops[{first[op['ref']]}] "
                             "- one swap_fp or renumber per part per list")
        first[op["ref"]] = i
    for i, op in enumerate(ops):
        j = first.get(op.get("ref"))
        if j is None or i == j:
            continue
        rk = ops[j]["op"]
        if op["op"] in REPAD_EXCLUSIVE:
            raise CheckError(
                f"ops[{i}] ({op['op']}): {op['ref']} is re-padded by "
                f"ops[{j}] ({rk}), which sets its pose and its pads' nets - "
                "give the pose to swap_fp (x, y, deg, side) and the nets to "
                "its `nets`, or run this op in a list of its own")
        if rk == "swap_fp" and i < j:
            raise CheckError(f"ops[{i}] ({op['op']}): comes before the "
                             f"swap_fp of {op['ref']} (ops[{j}]), which "
                             "replaces the footprint - put it after")


def _validate_set_pad(i: int, op: dict) -> None:
    pad = op["pad"]
    pads = [pad] if isinstance(pad, str) else pad
    if not (isinstance(pads, list) and pads
            and all(isinstance(p, str) and p for p in pads)):
        raise CheckError(f"ops[{i}]: pad must be a pad number or a list")
    if not op.keys() & {"net", "size", "zone_connection"}:
        raise CheckError(f"ops[{i}] (set_pad): needs net, size and/or "
                         "zone_connection")
    if "zone_connection" in op and op["zone_connection"] not in ZONE_CONNECT:
        raise CheckError(f"ops[{i}]: zone_connection must be one of "
                         f"{sorted(ZONE_CONNECT)}")
    if "net" in op and not isinstance(op["net"], str):
        raise CheckError(f"ops[{i}]: net must be a string (\"\" = no net)")
    if "size" in op:
        sz = op["size"]
        sz = [sz, sz] if not isinstance(sz, list) else sz
        if not (len(sz) == 2 and all(isinstance(v, (int, float))
                                     and not isinstance(v, bool)
                                     and 0 < v < 50 for v in sz)):
            raise CheckError(f"ops[{i}]: size must be mm or [w, h] mm, "
                             "0 < size < 50")


def _validate_set_text(i: int, op: dict) -> None:
    if not op.keys() & {"hide", "size", "thickness", "layer"}:
        raise CheckError(f"ops[{i}] (set_text): needs at least one of "
                         "hide, size, thickness, layer")
    if "hide" in op and not isinstance(op["hide"], bool):
        raise CheckError(f"ops[{i}]: hide must be a boolean")
    if "layer" in op and op["layer"] not in TEXT_LAYERS:
        raise CheckError(f"ops[{i}]: layer must be one of "
                         f"{sorted(TEXT_LAYERS)}")
    for k in ("size", "thickness"):
        if k in op and not (isinstance(op[k], (int, float))
                            and not isinstance(op[k], bool)
                            and 0 < op[k] < 20):
            raise CheckError(f"ops[{i}]: {k} out of range")
    # the floor holds unless the op explicitly sends the field to a fab
    # layer (fab text is never printed); without a layer the field is
    # assumed to stay on silk, which is where refdes fields live
    if op.get("layer", "F.SilkS") in SILK_LAYERS \
            and ("size" in op or "thickness" in op):
        min_size, min_th = silk_text_floor()
        if "size" in op and op["size"] + 1e-9 < min_size:
            raise CheckError(
                f"ops[{i}] (set_text): size {op['size']} mm is below the "
                f"{min_size} mm silk legibility floor (check_silk) - hide "
                "the field instead")
        if "thickness" in op and op["thickness"] + 1e-9 < min_th:
            raise CheckError(
                f"ops[{i}] (set_text): thickness {op['thickness']} mm is "
                f"below the {min_th} mm silk stroke floor (fab min silk "
                "width)")


def _expected_state(ops: list[dict]) -> dict[str, dict]:
    """Fold the op list into the final expected {ref: {x,y,deg,side,locked}}."""
    want: dict[str, dict] = {}
    for op in ops:
        if op["op"] in TEXT_OPS:
            continue  # verified independently by _verify_texts
        if op["op"] in ("silk_clear", "set_pad", "set_dnp", "renumber"):
            continue  # moves nothing; _verify_parts/_verify_repad check pads
        w = want.setdefault(op["ref"], {})
        if op["op"] == "swap_fp":  # resolved: _pose/_locked (_resolve_repad)
            pose = op.get("_pose") or {}
            for k in ("x", "y", "deg", "side"):
                v = op.get(k, pose.get(k))
                if v is not None:
                    w[k] = v
            if "_locked" in op:
                w["locked"] = op["_locked"]
        if op["op"] in ("place", "move"):
            w["x"], w["y"] = op["x"], op["y"]
        if op["op"] in ("place", "rotate") and op.get("deg") is not None:
            w["deg"] = op["deg"]
        if op["op"] in ("place", "flip") and op.get("side") is not None:
            w["side"] = op["side"]
        if op["op"] == "lock":
            w["locked"] = op["locked"]
    return want


def _verify(pcb: Path, ops: list[dict]) -> list[str]:
    model = placelib.PlaceModel(pcb)
    problems = []
    for ref, w in _expected_state(ops).items():
        fp = model.footprints.get(ref)
        if fp is None:
            problems.append(f"{ref}: vanished from saved board")
            continue
        if "x" in w and (abs(fp.pos[0] - w["x"]) > POS_TOL
                         or abs(fp.pos[1] - w["y"]) > POS_TOL):
            problems.append(f"{ref}: position {fp.pos} != ({w['x']}, {w['y']})")
        if "deg" in w and _angdiff(fp.angle, w["deg"]) > ANG_TOL:
            problems.append(f"{ref}: angle {fp.angle} != {w['deg']}")
        if "side" in w and fp.side != w["side"]:
            problems.append(f"{ref}: side {fp.side} != {w['side']}")
        if "locked" in w and fp.locked != w["locked"]:
            problems.append(f"{ref}: locked {fp.locked} != {w['locked']}")
    return problems


def _angdiff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


# ---------------------------------------------------------------- text verify
# Independent of the SWIG worker: sexpdata parse of the saved file. gr_text
# positions are board-frame; footprint (property ...) positions are LOCAL with
# an ABSOLUTE angle (LEARNINGS [geometry]) - transform abs = fp + R(-deg).local.

def _sx_head(n):
    import sexpdata
    return n[0].value() if isinstance(n, list) and n \
        and isinstance(n[0], sexpdata.Symbol) else None


def _sx_str(v) -> str:
    import sexpdata
    return v.value() if isinstance(v, sexpdata.Symbol) else str(v)


def _parse_board_texts(pcb: Path):
    """-> (gr_texts [{text, layer, x, y, deg}], fields {(ref, field): {x, y, deg}})."""
    import sexpdata
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    gr_texts, fields = [], {}
    for node in data[1:]:
        head = _sx_head(node)
        if head == "gr_text" and len(node) >= 2:
            entry = {"text": _sx_str(node[1]), "layer": None,
                     "x": None, "y": None, "deg": 0.0}
            for sub in node[2:]:
                sh = _sx_head(sub)
                if sh == "at":
                    entry["x"], entry["y"] = float(sub[1]), float(sub[2])
                    if len(sub) > 3:
                        entry["deg"] = float(sub[3])
                elif sh == "layer":
                    entry["layer"] = _sx_str(sub[1])
            gr_texts.append(entry)
        elif head == "footprint":
            fx = fy = fdeg = None
            props = {}
            for sub in node[1:]:
                sh = _sx_head(sub)
                if sh == "at":
                    fx, fy = float(sub[1]), float(sub[2])
                    fdeg = float(sub[3]) if len(sub) > 3 else 0.0
                elif sh == "property" and len(sub) >= 3:
                    pname = _sx_str(sub[1]).lower()
                    if pname in ("reference", "value"):
                        lx = ly = None
                        adeg = 0.0
                        for p in sub[3:]:
                            if _sx_head(p) == "at":
                                lx, ly = float(p[1]), float(p[2])
                                if len(p) > 3:
                                    adeg = float(p[3])
                        props[pname] = (_sx_str(sub[2]), lx, ly, adeg,
                                        _field_style(sub))
            if fx is None or "reference" not in props:
                continue
            ref = props["reference"][0]
            th = math.radians(fdeg or 0.0)
            for pname, (_, lx, ly, adeg, style) in props.items():
                if lx is None:
                    continue
                ax = fx + lx * math.cos(th) + ly * math.sin(th)
                ay = fy - lx * math.sin(th) + ly * math.cos(th)
                fields[(ref, pname)] = {"x": ax, "y": ay, "deg": adeg,
                                        **style}
    return gr_texts, fields


def _field_style(prop) -> dict:
    """{layer, hidden, size_x, size_y, thickness} of a footprint property.

    Hidden is `(hide yes)` on the property (KiCad 8+; `(hide no)` is shown),
    or the older bare `hide` token inside (effects ...)."""
    import sexpdata
    out = {"layer": None, "hidden": False, "size_x": None, "size_y": None,
           "thickness": None}
    for p in prop[3:]:
        h = _sx_head(p)
        if h == "layer" and len(p) > 1:
            out["layer"] = _sx_str(p[1])
        elif h == "hide":
            out["hidden"] = not (len(p) > 1 and _sx_str(p[1]) == "no")
        elif h == "effects":
            for e in p[1:]:
                if isinstance(e, sexpdata.Symbol) and e.value() == "hide":
                    out["hidden"] = True
                if _sx_head(e) != "font":
                    continue
                for f in e[1:]:
                    fh = _sx_head(f)
                    if fh == "size" and len(f) >= 3:
                        out["size_x"], out["size_y"] = float(f[1]), float(f[2])
                    elif fh == "thickness" and len(f) >= 2:
                        out["thickness"] = float(f[1])
    return out


def _verify_set_text(op: dict, got: dict | None) -> list[str]:
    tag = f"set_text {op['ref']}.{op['field']}"
    if got is None:
        return [f"{tag}: field not found in saved board"]
    problems = []
    if "hide" in op and got["hidden"] != op["hide"]:
        problems.append(f"{tag}: hidden {got['hidden']} != {op['hide']}")
    if "layer" in op and got["layer"] != op["layer"]:
        problems.append(f"{tag}: layer {got['layer']} != {op['layer']}")
    if "size" in op and (got["size_y"] is None
                         or abs(got["size_x"] - op["size"]) > SIZE_TOL
                         or abs(got["size_y"] - op["size"]) > SIZE_TOL):
        problems.append(f"{tag}: size ({got['size_x']}, {got['size_y']}) "
                        f"!= {op['size']}")
    if "thickness" in op and (got["thickness"] is None
                              or abs(got["thickness"] - op["thickness"])
                              > SIZE_TOL):
        problems.append(f"{tag}: thickness {got['thickness']} != "
                        f"{op['thickness']}")
    return problems


def _verify_texts(pcb: Path, ops: list[dict]) -> list[str]:
    text_ops = [op for op in ops if op["op"] in TEXT_OPS]
    if not text_ops:
        return []
    problems = []
    gr_texts, fields = _parse_board_texts(pcb)
    for op in text_ops:
        if op["op"] in ("add_text", "remove_text"):
            hits = [t for t in gr_texts
                    if t["text"] == op["text"] and t["layer"] == op["layer"]
                    and abs(t["x"] - op["x"]) <= POS_TOL
                    and abs(t["y"] - op["y"]) <= POS_TOL]
            if op["op"] == "remove_text":
                if hits:
                    problems.append(
                        f"remove_text '{op['text']}': {len(hits)} still on "
                        f"{op['layer']} at ({op['x']}, {op['y']})")
                continue
            if not hits:
                problems.append(
                    f"add_text '{op['text']}' not found on {op['layer']} at "
                    f"({op['x']}, {op['y']})")
            elif len(hits) > 1:
                problems.append(f"add_text '{op['text']}': {len(hits)} "
                                f"duplicates at the target position")
            elif op.get("deg") is not None \
                    and _angdiff(hits[0]["deg"], op["deg"]) > ANG_TOL:
                problems.append(f"add_text '{op['text']}': angle "
                                f"{hits[0]['deg']} != {op['deg']}")
        elif op["op"] == "set_text":
            problems += _verify_set_text(op, fields.get((op["ref"],
                                                         op["field"])))
        else:
            got = fields.get((op["ref"], op["field"]))
            if got is None:
                problems.append(f"move_text {op['ref']}.{op['field']}: "
                                f"field not found in saved board")
            elif (abs(got["x"] - op["x"]) > POS_TOL
                  or abs(got["y"] - op["y"]) > POS_TOL):
                problems.append(
                    f"move_text {op['ref']}.{op['field']}: position "
                    f"({got['x']:.4f}, {got['y']:.4f}) != ({op['x']}, {op['y']})")
            elif op.get("deg") is not None \
                    and _angdiff(got["deg"], op["deg"]) > ANG_TOL:
                problems.append(f"move_text {op['ref']}.{op['field']}: angle "
                                f"{got['deg']} != {op['deg']}")
    return problems


def _dnp_refs(pcb: Path) -> set[str]:
    """Refs whose footprint carries (attr ... dnp) in the saved file."""
    import sexpdata
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    out = set()
    for node in data[1:]:
        if _sx_head(node) != "footprint":
            continue
        ref = dnp = None
        for sub in node[1:]:
            h = _sx_head(sub)
            if h == "property" and len(sub) >= 3 \
                    and _sx_str(sub[1]).lower() == "reference":
                ref = _sx_str(sub[2])
            elif h == "attr":
                dnp = any(_sx_str(a) == "dnp" for a in sub[1:])
        if ref and dnp:
            out.add(ref)
    return out


def _pad_zone_connects(pcb: Path, ref: str) -> dict[str, list]:
    """{pad number: [zone_connect of each pad with it, None = inherit]} of
    one footprint in the saved file (a shell has several pads numbered S1)."""
    import sexpdata
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    for node in data[1:]:
        if _sx_head(node) != "footprint":
            continue
        if not any(_sx_head(s) == "property" and len(s) >= 3
                   and _sx_str(s[1]).lower() == "reference"
                   and _sx_str(s[2]) == ref for s in node[1:]):
            continue
        out: dict[str, list] = {}
        for pad in node[1:]:
            if _sx_head(pad) != "pad" or len(pad) < 2:
                continue
            zc = next((int(s[1]) for s in pad[2:]
                       if _sx_head(s) == "zone_connect" and len(s) > 1), None)
            out.setdefault(_sx_str(pad[1]), []).append(zc)
        return out
    return {}


def _verify_parts(pcb: Path, ops: list[dict]) -> list[str]:
    """set_pad / set_dnp landed, read back independently of the worker."""
    part_ops = [op for op in ops if op["op"] in ("set_pad", "set_dnp")]
    if not part_ops:
        return []
    import geom
    bg = geom.BoardGeom.from_file(pcb)
    dnp = _dnp_refs(pcb) if any(o["op"] == "set_dnp" for o in part_ops) \
        else set()
    problems = []
    for op in part_ops:
        if op["op"] == "set_dnp":
            if (op["ref"] in dnp) != op["dnp"]:
                problems.append(f"set_dnp {op['ref']}: dnp is "
                                f"{op['ref'] in dnp}")
            continue
        nums = [op["pad"]] if isinstance(op["pad"], str) else op["pad"]
        if "zone_connection" in op:
            want = ZONE_CONNECT[op["zone_connection"]]
            have = _pad_zone_connects(pcb, op["ref"])
            for n in nums:
                if any(z != want for z in have.get(n, [want])):
                    problems.append(
                        f"set_pad {op['ref']}.{n}: zone_connect "
                        f"{have.get(n)} != {op['zone_connection']}")
        got = {p.number: p for p in bg.pads_of(ref=op["ref"])}
        for n in nums:
            p = got.get(n)
            if p is None:
                problems.append(f"set_pad {op['ref']}.{n}: pad not found")
                continue
            if "net" in op and (p.net or "") != op["net"]:
                problems.append(f"set_pad {op['ref']}.{n}: net {p.net!r} "
                                f"!= {op['net']!r}")
            if "size" in op:
                sz = op["size"] if isinstance(op["size"], list) \
                    else [op["size"]] * 2
                if abs(p.size[0] - sz[0]) > SIZE_TOL \
                        or abs(p.size[1] - sz[1]) > SIZE_TOL:
                    problems.append(f"set_pad {op['ref']}.{n}: size "
                                    f"{p.size} != {tuple(sz)}")
    return problems


def _refuse_routed_renet(pcb: Path, ops: list[dict]) -> None:
    """set_pad net on a pad copper touches is refused: KiCad re-derives that
    copper's net from the pad when it saves, so the track would follow the
    pad onto the new net and short whatever else it reaches."""
    renet = [op for op in ops if op["op"] == "set_pad" and "net" in op]
    if not renet:
        return
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import geom
    import route_edit
    bg = geom.BoardGeom.from_file(pcb)
    for op in renet:
        nums = [op["pad"]] if isinstance(op["pad"], str) else op["pad"]
        hit = route_edit.select_items(
            bg, {"pads": [f"{op['ref']}.{n}" for n in nums]})
        if hit:
            raise CheckError(
                f"set_pad {op['ref']} {nums}: {len(hit)} tracks/vias touch "
                "these pads and would follow them onto the new net - rip "
                "them first (route_edit --op '{\"op\": \"rip\", "
                f"\"pads\": [\"{op['ref']}.{nums[0]}\"]}}'), or rename the "
                "whole net with route_edit rename_net")


# ------------------------------------------------------------- re-pad ops
# swap_fp / renumber are resolved HERE, against the board as it is before the
# op list, and the worker only carries them out: which file it loads, the net
# each new pad takes, the zone connection that follows a pad number, where
# every pad lands - and that no copper of another net touches one of them.

ZONE_WORD = {v: k for k, v in ZONE_CONNECT.items()}  # 2 -> "solid", ...


def _fp_nodes(data, ref: str) -> list:
    """The (footprint ...) nodes of a parsed board whose Reference is REF."""
    return [n for n in data[1:] if _sx_head(n) == "footprint"
            and any(_sx_head(s) == "property" and len(s) >= 3
                    and _sx_str(s[1]).lower() == "reference"
                    and _sx_str(s[2]) == ref for s in n[1:])]


def _node_uuid(node) -> str | None:
    return next((_sx_str(s[1]) for s in node[1:]
                 if _sx_head(s) == "uuid" and len(s) > 1), None)


def _grouped(data) -> set[str]:
    """uuids that are members of a (group ...) on the board."""
    return {_sx_str(m) for g in data[1:] if _sx_head(g) == "group"
            for s in g[1:] if _sx_head(s) == "members" for m in s[1:]}


def _net_class(net: str | None) -> str:
    """board_update._nets_equal's no-connect class: "" for an open pad."""
    net = net or ""
    return "" if net.startswith("unconnected-") else net


def _old_net(old_pads: list, number: str, ref: str, kind: str) -> str | None:
    """The net the old pads numbered NUMBER share (None: no such pad)."""
    have = [p.net or "" for p in old_pads if p.number == number]
    if not have:
        return None
    if len({_net_class(n) for n in have}) > 1:
        raise CheckError(
            f"{kind} {ref}: the old pads numbered {number} are on nets "
            f"{sorted(set(have))} - say which one with nets "
            f"{{\"{number}\": NET}}")
    return next((n for n in have if _net_class(n)), have[0])


def _uuid_hint(items: list) -> str:
    return json.dumps({"op": "rip", "uuid_prefix": sorted(
        {(it.uuid or "?")[:8] for it in items})})


def _resolve_repad(pcb: Path, ops: list[dict], stage: Path, *,
                   touch_check: bool = True
                   ) -> tuple[list[dict], dict[int, list[str]]]:
    """-> (the op list with each swap_fp/renumber resolved into `_` fields
    for the worker and _verify_repad, notes {op index: [str]}).

    CheckError, before anything is staged, when a re-pad cannot be done
    safely: the part is not on the board exactly once or sits in a group,
    the file is not a footprint, old pads of one number disagree on the net,
    a renumber `at` finds no pad or the same pad twice, a `nets` key names
    no pad - or copper of another net touches a pad the op produces
    (touch_check=False skips that last one: part_repair.py's trial run on a
    scratch copy, which only reads the pads back and rips that copper)."""
    if not any(op["op"] in REPAD_OPS for op in ops):
        return ops, {}
    import copy

    import sexpdata
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import board_update
    import geom
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    bg = geom.BoardGeom.from_file(pcb)
    model = placelib.PlaceModel(pcb)
    grouped = _grouped(data)
    dnp = _dnp_refs(pcb)
    wops = copy.deepcopy(ops)
    notes: dict[int, list[str]] = {}
    for i, op in enumerate(wops):
        kind = op["op"]
        if kind not in REPAD_OPS:
            continue
        ref, note = op["ref"], notes.setdefault(i, [])
        nodes = _fp_nodes(data, ref)
        if len(nodes) != 1:
            raise CheckError(f"{kind} {ref}: {len(nodes)} footprints carry "
                             "this reference - it must be exactly one")
        uuid = _node_uuid(nodes[0])
        if uuid in grouped:
            raise CheckError(f"{kind} {ref}: the part is in a group - "
                             "ungroup it first (the swap does not carry "
                             "group membership)")
        if _fp_nodes(data, ref + "__swap_old"):
            raise CheckError(f"{kind} {ref}: {ref}__swap_old is on the "
                             "board (a broken earlier swap) - remove it")
        old = [p for p in bg.pads_of(ref=ref)]
        zc = _pad_zone_connects(pcb, ref)
        fp = model.footprints[ref]
        given = op.get("nets") or {}
        if kind == "swap_fp":
            src = Path(op["footprint"]).expanduser().resolve()
            try:
                root = sexpdata.loads(src.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                raise CheckError(f"swap_fp {ref}: cannot read "
                                 f"{op['footprint']}: {e}") from e
            except Exception as e:  # noqa: BLE001 - sexpdata's parse errors
                raise CheckError(f"swap_fp {ref}: {op['footprint']} is not "
                                 f"an s-expression: {e}") from e
            if _sx_head(root) not in ("footprint", "module"):
                raise CheckError(f"swap_fp {ref}: {op['footprint']} is not "
                                 "a footprint file")
            lib_dir = stage / f"swap{i}.pretty"
            lib_dir.mkdir()
            shutil.copy2(src, lib_dir / src.name)
            op["_lib"], op["_name"] = str(lib_dir), src.stem
            lib = src.parent.name[:-len(".pretty")] \
                if src.parent.name.endswith(".pretty") \
                else fp.fpid.split(":", 1)[0]
            op["_fpid"] = op.get("fpid") or f"{lib}:{src.stem}"
            op["_pose"] = {"x": op.get("x", fp.pos[0]),
                           "y": op.get("y", fp.pos[1]),
                           "deg": op.get("deg", fp.angle),
                           "side": op.get("side", fp.side)}
            op["_locked"], op["_dnp"], op["_uuid"] = fp.locked, \
                ref in dnp, uuid
            probe = board_update.placed_pads(bg, lib_dir / src.name, ref,
                                             op["_pose"], {})
            if not probe:
                raise CheckError(f"swap_fp {ref}: {op['footprint']} has no "
                                 "copper pads")
            numbers = sorted({p.number for p in probe if p.number})
            unknown = sorted(set(given) - set(numbers))
            if unknown:
                raise CheckError(f"swap_fp {ref}: nets names pad(s) "
                                 f"{unknown} the new footprint lacks "
                                 f"(it has {numbers})")
            nets = {}
            for n in numbers:
                was = _old_net(old, n, ref, kind)
                nets[n] = given[n] if n in given else (was or "")
                if was is None and n not in given:
                    note.append(f"new pad {n} had no old pad: no net")
            op["_nets"] = nets
            for n in sorted({p.number for p in old if p.number}
                            - set(numbers)):
                was = _old_net(old, n, ref, kind)
                if _net_class(was):
                    note.append(f"old pad {n} ({was}) has no pad in the "
                                "new footprint: its net loses this pin")
            op["_zone"] = {}
            for n in numbers:
                have = set(zc.get(n, [None]))
                if len(have) == 1 and None not in have:
                    z = have.pop()
                    if z in ZONE_WORD:
                        op["_zone"][n] = ZONE_WORD[z]
                    else:
                        note.append(f"pad {n}: zone_connect {z} not carried")
            final = [dataclasses.replace(p, net=nets.get(p.number) or None)
                     for p in probe]
            expect_zone = {n: ZONE_CONNECT[w] for n, w in op["_zone"].items()}
        else:
            final = list(old)
            # used: old-pad index -> the zone connection it will have
            # ("?" = unknown: it keeps its own)
            pads, used = [], {}
            for spec in op["pads"]:
                at = spec["at"]
                hit = [k for k, p in enumerate(old)
                       if math.dist(p.center, at) <= REPAD_TOL]
                if len(hit) != 1:
                    raise CheckError(
                        f"renumber {ref}: {len(hit)} of its copper pads lie "
                        f"within {REPAD_TOL} mm of {at} - it must be one "
                        "(pad centres: " + ", ".join(
                            f"{p.number}@{[round(c, 4) for c in p.center]}"
                            for p in old) + ")")
                k = hit[0]
                if k in used:
                    raise CheckError(f"renumber {ref}: pad {old[k].number} "
                                     f"at {at} is named twice")
                used[k] = "?"
                n = spec["number"]
                was = _old_net(old, n, ref, kind)
                net = given[n] if n in given else (was or "")
                if was is None and n not in given:
                    note.append(f"no old pad was numbered {n}: no net")
                entry = {"at": list(old[k].center), "number": n, "net": net}
                have = set(zc.get(n, []))
                if len(have) == 1:
                    z = next(iter(have))
                    if z in ZONE_WORD:
                        entry["zone"] = ZONE_WORD[z]
                        used[k] = z
                elif have:
                    note.append(f"old pads numbered {n} disagree on the zone "
                                f"connection: pad at {at} keeps its own")
                pads.append(entry)
                final[k] = dataclasses.replace(old[k], number=n,
                                               net=net or None)
            unknown = sorted(set(given) - {e["number"] for e in pads})
            if unknown:
                raise CheckError(f"renumber {ref}: nets names pad(s) "
                                 f"{unknown} no `pads` entry gives")
            op["_pads"] = pads
            per: dict[str, set] = {}
            for k, p in enumerate(final):
                if k in used:
                    per.setdefault(p.number, set()).add(used[k])
                else:
                    was = set(zc.get(p.number, [None]))
                    per.setdefault(p.number, set()).add(
                        was.pop() if len(was) == 1 else "?")
            expect_zone = {n: v.pop() for n, v in per.items()
                           if len(v) == 1 and "?" not in v}
            dup = sorted(n for n in {p.number for p in final}
                         if sum(q.number == n for q in final)
                         > sum(q.number == n for q in old))
            if dup:
                note.append(f"pad numbers {dup} now name more pads than "
                            "before")
        op["_expect"] = sorted([p.number, p.net or "", round(p.center[0], 6),
                                round(p.center[1], 6)] for p in final)
        op["_expect_zone"] = expect_zone
        short = []
        for p in final:
            for it in board_update._touching(bg, p):
                if not board_update._nets_equal(it.net, p.net):
                    short.append((p, it))
        if short and touch_check:
            shown = "; ".join(
                f"pad {p.number} ({p.net or 'no net'}) <- {it.net} "
                f"{'track' if isinstance(it, geom.Track) else 'via'} "
                f"{(it.uuid or '?')[:8]}" for p, it in short[:6])
            more = f" and {len(short) - 6} more" if len(short) > 6 else ""
            raise CheckError(
                f"{kind} {ref}: copper of other nets touches the pads it "
                f"would produce ({shown}{more}); KiCad would carry it onto "
                "the pad's net when it saves - rip it first (route_edit "
                f"--op '{_uuid_hint([it for _, it in short])}'), re-route "
                "after: part_repair.py does both and checks the result")
        stub = {id(it): it for p in old for it in board_update._touching(bg, p)}
        for p in final:
            for it in board_update._touching(bg, p):
                stub.pop(id(it), None)
        if stub:
            note.append(f"{len(stub)} tracks/vias touched the old pads and "
                        "touch no new pad: they are left dangling ("
                        f"{_uuid_hint(list(stub.values()))})")
        if not note:
            notes.pop(i)
    return wops, notes


def _verify_repad(pcb: Path, ops: list[dict]) -> list[str]:
    """swap_fp / renumber landed, read back from the saved file: one part
    with the ref (the old one gone), its library id, uuid, DNP, and every
    copper pad's number, net and position, and the zone connections."""
    done = [op for op in ops if op["op"] in REPAD_OPS]
    if not done:
        return []
    import sexpdata

    import geom
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    bg = geom.BoardGeom.from_file(pcb)
    dnp = _dnp_refs(pcb)
    problems = []
    for op in done:
        kind, ref = op["op"], op["ref"]
        nodes = _fp_nodes(data, ref)
        if len(nodes) != 1:
            problems.append(f"{kind} {ref}: {len(nodes)} footprints carry "
                            "the reference")
            continue
        if _fp_nodes(data, ref + "__swap_old"):
            problems.append(f"{kind} {ref}: the old footprint is still on "
                            "the board")
        if kind == "swap_fp":
            got = _sx_str(nodes[0][1]) if len(nodes[0]) > 1 else ""
            if got != op["_fpid"]:
                problems.append(f"swap_fp {ref}: library id {got!r} != "
                                f"{op['_fpid']!r}")
            if op.get("_uuid") and _node_uuid(nodes[0]) != op["_uuid"]:
                problems.append(f"swap_fp {ref}: uuid {_node_uuid(nodes[0])}"
                                f" != the old part's {op['_uuid']}")
            if not any(o["op"] == "set_dnp" and o["ref"] == ref
                       for o in ops) and (ref in dnp) != op["_dnp"]:
                problems.append(f"swap_fp {ref}: dnp is {ref in dnp}")
        have = sorted([p.number, p.net or "", p.center[0], p.center[1]]
                      for p in bg.pads_of(ref=ref))
        want = op["_expect"]
        if len(have) != len(want):
            problems.append(f"{kind} {ref}: {len(have)} copper pads, "
                            f"expected {len(want)}")
        else:
            left = list(have)
            for n, net, x, y in want:
                k = next((k for k, h in enumerate(left) if h[0] == n
                          and math.dist(h[2:], (x, y)) <= POS_TOL), None)
                if k is None:
                    problems.append(f"{kind} {ref}: no pad {n} at "
                                    f"({x}, {y})")
                    continue
                if not _nets_equal(left[k][1], net):
                    problems.append(f"{kind} {ref}: pad {n} at ({x}, {y}) "
                                    f"is on {left[k][1]!r}, not {net!r}")
                left.pop(k)
        zc = _pad_zone_connects(pcb, ref)
        for n, z in (op.get("_expect_zone") or {}).items():
            if any(v != z for v in zc.get(n, [z])):
                problems.append(f"{kind} {ref}: pad {n} zone_connect "
                                f"{zc.get(n)} != {z}")
    return problems


def _nets_equal(a: str | None, b: str | None) -> bool:
    return _net_class(a) == _net_class(b)


def apply_ops(pcb: Path, ops: list[dict], *, refill: bool = False,
              touch_check: bool = True) -> dict:
    """Validate refs, stage, run the SWIG worker, verify, atomically swap in.

    Returns the worker's per-op results. Raises CheckError (original board
    untouched) on any failure. Importable - place_seed --apply uses this.
    touch_check=False lets a swap_fp/renumber land on other-net copper
    (part_repair.py's trial on a scratch copy; never on a board you keep).
    """
    pcb = Path(pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    model = placelib.PlaceModel(pcb)
    missing = sorted({op["ref"] for op in ops if "ref" in op}
                     - model.footprints.keys())
    if missing:
        raise CheckError(f"refs not on board: {', '.join(missing)}")
    _refuse_routed_renet(pcb, ops)

    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")

    stage = Path(tempfile.mkdtemp(prefix=".aiee_edit_", dir=pcb.parent))
    try:
        # swap_fp / renumber: resolved against the board before the list
        wops, notes = _resolve_repad(pcb, ops, stage,
                                     touch_check=touch_check)
        staged = stage / pcb.name
        shutil.copy2(pcb, staged)
        job = {"board": str(staged), "out": str(staged), "ops": wops}
        jf = stage / "job.json"
        jf.write_text(json.dumps(job), encoding="utf-8")
        cp = subprocess.run([str(bp), str(WORKER), str(jf)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
        # Scan BACKWARDS for the last parseable JSON object rather than
        # trusting the last line: KiCad's SWIG runtime prints
        # "swig/python detected a memory leak of type 'PCB_SHAPE *'" lines at
        # interpreter shutdown - i.e. AFTER the worker's own output - whenever
        # an op detached an item from the board (silk_clear), and taking
        # out[-1] then reported a clean run as "worker exit 0 (rolled back)".
        # Same shape as board_init._last_json.
        result = {}
        for line in reversed((cp.stdout or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
        if cp.returncode != 0 or not result.get("ok"):
            detail = result.get("error") or (cp.stderr or "").strip()[-300:] \
                or f"worker exit {cp.returncode}"
            idx = result.get("index")
            at = f" at ops[{idx}]" if idx is not None else ""
            raise CheckError(f"worker failed{at}: {detail} (rolled back)")
        problems = _verify(staged, wops) + _verify_texts(staged, ops) \
            + _verify_parts(staged, ops) + _verify_repad(staged, wops)
        if problems:
            raise CheckError("post-apply verify failed (rolled back): "
                             + "; ".join(problems))
        if refill:  # a pad's zone connection or size changes the fill
            if str(SCRIPTS) not in sys.path:
                sys.path.insert(0, str(SCRIPTS))
            import route_edit
            route_edit._refill(cli, pcb, staged)
        os.replace(staged, pcb)
        results = result["results"]
        for i, extra in notes.items():
            if i < len(results) and isinstance(results[i], dict):
                results[i].setdefault("notes", []).extend(extra)
        return results
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def load_ops(ops_file: str | None, inline: list[str]) -> list[dict]:
    """--ops FILE and/or --op JSON (repeatable) -> validated op list."""
    ops: list = []
    if ops_file:
        doc = checklib.load_json(ops_file, "ops file")
        if not isinstance(doc, dict) or doc.get("version") != 1 \
                or not isinstance(doc.get("ops"), list):
            raise CheckError("ops file must be {'version': 1, 'ops': [...]}")
        ops += doc["ops"]
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
    ap.add_argument("--allow-routed", action="store_true",
                    help="permit footprint ops on a board that already has "
                         "tracks/vias (they invalidate the gate place / "
                         "check_decoupling oracles - rip the affected nets "
                         "first and re-run drc_routed after)")
    ap.add_argument("--refill", action="store_true",
                    help="refill zones on the staged copy before the swap "
                         "(after set_pad zone_connection/size)")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    ops = load_ops(args.ops, args.op)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    # T6 P6A-4 (ladder row 130): a courtyard-legal footprint move on a ROUTED
    # board shorted a board while gate place + check_decoupling stayed green.
    # Text ops (silk sweeps) stay legitimate on routed boards and are exempt.
    fp_ops = [op for op in ops if op["op"] in FOOTPRINT_OPS]
    routed = board_routed(pcb) if fp_ops else False
    if fp_ops and routed and not args.allow_routed:
        raise CheckError(
            "board has routing - footprint moves invalidate the gate place / "
            "check_decoupling oracles (LEARNINGS 2026-07-30: rip the "
            "affected nets FIRST, then move, then route fresh). Pass "
            "--allow-routed to proceed deliberately and re-run drc_routed "
            "after")
    results = apply_ops(pcb, ops, refill=args.refill)
    payload = {"script": "place_edit", "status": "pass", "board": args.pcb,
               "applied": len(ops), "verified": True,
               "refilled": args.refill, "results": results}
    if fp_ops and routed:
        payload["routed_board"] = True
        payload["warnings"] = ["footprint ops applied on a routed board "
                               "(--allow-routed): re-run the drc_routed gate"]
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("place_edit", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
