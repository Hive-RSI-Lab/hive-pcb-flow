"""place_swig - BUNDLED-python SWIG worker for place_edit.py (S9).

Runs inside KiCad's bundled python (the only interpreter with pcbnew); invoked
as `python place_swig.py job.json` by the venv driver. stdlib + lib/safelib.
Every job runs under safelib.board_locks(job): an OS writer lock on the
"board" and "out" .kicad_pcb (<pcb>.lock), held from LoadBoard to Save;
a second worker on the same board waits, then refuses (LockBusy).

job JSON: {"board": in_path, "out": out_path, "ops": [op, ...]}
ops are ABSOLUTE (idempotent by construction - re-applying is a no-op):
  {"op": "place",  "ref": R, "x": mm, "y": mm, ["deg": d], ["side": front|back]}
  {"op": "move",   "ref": R, "x": mm, "y": mm}
  {"op": "rotate", "ref": R, "deg": d}
  {"op": "flip",   "ref": R, "side": "front"|"back"}   # absolute side, not toggle
  {"op": "lock",   "ref": R, "locked": true|false}
Text ops (S14, closes V17 - silk labels/refdes moves were previously
unscriptable):
  {"op": "add_text",  "text": T, "x": mm, "y": mm, "layer": "F.SilkS",
   ["deg": d], ["size": mm], ["thickness": mm]}
     - board-frame graphic text. Idempotent: an existing text with the same
       string on the same layer within 0.01 mm is UPDATED, never duplicated.
  {"op": "remove_text", "text": T, "x": mm, "y": mm, "layer": "F.SilkS"}
     - deletes the board-frame graphic text matching (string, layer, position
       within 0.01 mm).  Idempotent: absent -> no-op, {"removed": 0}.  This is
       the only scripted way to RELOCATE a gr_text (remove + add_text), which
       a mismarked polarity/cathode legend needs.
  {"op": "move_text", "ref": R, "field": "reference"|"value", "x": mm,
   "y": mm, ["deg": d]}
     - repositions a footprint's Reference/Value field text (board frame;
       stored angle is ABSOLUTE per LEARNINGS [geometry]).
  {"op": "set_text", "ref": R, "field": "reference"|"value", ["hide": bool],
   ["size": mm], ["thickness": mm], ["layer": "F.SilkS"|"B.SilkS"|...]}
     - hides/shows, resizes (both axes) or re-layers a footprint's
       Reference/Value field in place; a B.* layer mirrors the text, an F.*
       layer un-mirrors it (same rule as add_text). The driver enforces the
       silk size/stroke floor. Absolute, so idempotent.
  {"op": "silk_clear", "ref": R, ["layer": "F.SilkS"], ["only_offboard": true],
   ["region": [x0, y0, x1, y1]]}
     - deletes a footprint's own GRAPHIC silk (lines/arcs/circles/polys) on
       that layer, never its Reference/Value text. `only_offboard` keeps the
       items fully inside the board outline; `region` (board mm) deletes
       only the items lying wholly inside that box. `over_pads` deletes only
       the items within 0.1 mm of one of the part's own pads; `edge_mm`
       only those closer than that to the board-edge box or past it; `at`
       [x, y] (board mm) only the one item whose box (grown 0.05 mm) holds
       that point - where DRC put a silk finding; `uuid` [..] only the items
       with those uuids (DRC reports name them). Filters combine (all must
       hold). The only scripted way to fix
       footprint-INTERNAL silk on an already-placed board (a library edit
       cannot reach one without re-running board_init and losing placement).

Pad and part-attribute ops (U-ECO: the hi-fi boards scripted these by hand):
  {"op": "set_pad", "ref": R, "pad": N | [N...], ["net": NAME | ""],
   ["size": mm | [w, h]], ["zone_connection": "solid"|"thermal"|"none"|
   "inherit"]}
     - re-nets (a net not on the board is created; "" clears it, for an NC
       pin), resizes and/or sets the zone connection of the named pads
       (solid = no thermal spokes, for a pad carrying amps into a plane;
       inherit = the footprint's/zone's setting again). Zones are not
       refilled here: refill after. Absolute, so idempotent.
  {"op": "set_dnp", "ref": R, "dnp": bool}

Re-pad ops (part_repair), resolved by the driver (place_edit._resolve_repad)
into `_` fields this worker only carries out:
  {"op": "swap_fp", "ref": R, "_lib": DIR.pretty, "_name": N, "_fpid": "L:N",
   "_pose": {x, y, deg, side}, "_nets": {number: net | ""},
   "_zone": {number: word}, "_locked": bool, "_uuid": U}
     - loads the footprint N from DIR in place of R's, as KiCad's "update
       footprint from library" does: the new part takes R's reference,
       value, fields, schematic link, field texts (where they sat), DNP,
       lock and uuid; its pads take `_nets` and the old pads' pin data by
       number, `_zone` where given. The old part is renamed R__swap_old and
       removed after the last op, right before Save (every removal last).
  {"op": "renumber", "ref": R, "_pads": [{"at": [x, y], "number": N,
   "net": NAME | "", ["zone": word]}]}
     - the pad at each board position (0.005 mm) takes the number, net,
       pin data (of the old pad with that number) and zone word given.

The board is saved ONLY if every op applied; any failure exits 3 with a JSON
error and writes nothing (the driver treats the staged copy as garbage).
Save() may drop a default .kicad_pro next to `out` - the driver stages in a
scratch dir and moves only the .kicad_pcb back, so real project files are
never clobbered. mm->IU uses pcbIUScale.mmToIU (FromMM truncates: prior-
attempt fact, e.g. 32.3 mm -> 32299999 nm).
"""
import json
import math
import sys

import safelib  # lib/ is sys.path[0]: the worker runs as lib/place_swig.py

import pcbnew

import update_swig  # apply_fields / inherit / net_of: board_update's swap


def iu(mm: float) -> int:
    return int(pcbnew.pcbIUScale.mmToIU(float(mm)))


def side_of(fp) -> str:
    return "back" if fp.GetLayer() == pcbnew.B_Cu else "front"


# TOP_BOTTOM, deliberately (U19). The two directions are NOT interchangeable
# for an absolute-op writer (measured on 10.0.3, usbbuck4 J1 at -90 deg):
#   LEFT_RIGHT mirrors in the BOARD frame and KEEPS the orientation, so the
#     resulting LOCAL frame is R(a).M_x.R(-a).L - it depends on the angle the
#     part happened to have when the op ran, and the same {x,y,deg,side} op
#     would then produce different geometry on different boards.
#   TOP_BOTTOM mirrors local y and negates the orientation, independent of the
#     starting angle; SetOrientationDegrees below then pins the angle, so the
#     op fully determines the result. placelib.Footprint.mirror models THIS.
# The name is FLIP_DIRECTION_TOP_BOTTOM (underscored) - an earlier
# un-underscored guess never resolved and always fell through to the bool
# fallback, which happens to mean the same thing here but silently.
_FLIP = getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", True)


def set_side(fp, want: str) -> None:
    if side_of(fp) != want:
        fp.Flip(fp.GetPosition(), _FLIP)


def _layer_id(board, name: str) -> int:
    lid = board.GetLayerID(name)
    if lid < 0:
        raise KeyError(f"unknown layer '{name}'")
    return lid


def _match_texts(board, op: dict) -> list:
    """Board-frame PCB_TEXTs matching (layer, string, position +-0.01 mm)."""
    lid = _layer_id(board, op["layer"])
    tol = iu(0.01)
    return [d for d in board.GetDrawings()
            if isinstance(d, pcbnew.PCB_TEXT) and d.GetLayer() == lid
            and d.GetText() == op["text"]
            and abs(d.GetPosition().x - iu(op["x"])) <= tol
            and abs(d.GetPosition().y - iu(op["y"])) <= tol]


def apply_text_op(board, op: dict) -> dict:
    kind = op["op"]
    if kind == "remove_text":
        hits = _match_texts(board, op)
        for d in hits:            # idempotent: absent -> removed 0, no error
            # RemoveNative, NOT Remove: Remove() hands ownership to python and
            # once that proxy is collected `board.Drawings()` comes back as a
            # bare SwigPyObject, so every later GetDrawings() raises
            # "'SwigPyObject' object is not iterable" (KiCad 10.0.3, measured).
            board.RemoveNative(d)
        return {"text": op["text"], "layer": op["layer"],
                "x": float(op["x"]), "y": float(op["y"]),
                "removed": len(hits)}
    if kind == "add_text":
        lid = _layer_id(board, op["layer"])
        hits = _match_texts(board, op)
        txt = hits[0] if hits else None  # idempotent re-apply: update, no dupe
        if txt is None:
            txt = pcbnew.PCB_TEXT(board)
            board.Add(txt)
        txt.SetText(op["text"])
        txt.SetLayer(lid)
        txt.SetPosition(pcbnew.VECTOR2I(iu(op["x"]), iu(op["y"])))
        if op.get("deg") is not None:
            txt.SetTextAngleDegrees(float(op["deg"]))
        size = float(op.get("size", 1.0))
        txt.SetTextSize(pcbnew.VECTOR2I(iu(size), iu(size)))
        txt.SetTextThickness(iu(float(op.get("thickness", 0.15))))
        if op["layer"].startswith("B."):
            txt.SetMirrored(True)
        pos = txt.GetPosition()
        return {"text": op["text"], "layer": op["layer"],
                "x": round(pcbnew.ToMM(pos.x), 6),
                "y": round(pcbnew.ToMM(pos.y), 6),
                "deg": round(txt.GetTextAngleDegrees(), 4)}
    if kind == "move_text":
        fp = board.FindFootprintByReference(op["ref"])
        if fp is None:
            raise KeyError(f"footprint '{op['ref']}' not on board")
        item = fp.Reference() if op["field"] == "reference" else fp.Value()
        item.SetPosition(pcbnew.VECTOR2I(iu(op["x"]), iu(op["y"])))
        if op.get("deg") is not None:
            item.SetTextAngleDegrees(float(op["deg"]))
        pos = item.GetPosition()
        return {"ref": op["ref"], "field": op["field"],
                "x": round(pcbnew.ToMM(pos.x), 6),
                "y": round(pcbnew.ToMM(pos.y), 6),
                "deg": round(item.GetTextAngleDegrees(), 4)}
    if kind == "set_text":
        fp = board.FindFootprintByReference(op["ref"])
        if fp is None:
            raise KeyError(f"footprint '{op['ref']}' not on board")
        item = fp.Reference() if op["field"] == "reference" else fp.Value()
        if op.get("layer") is not None:
            item.SetLayer(_layer_id(board, op["layer"]))
            item.SetMirrored(op["layer"].startswith("B."))
        if op.get("size") is not None:
            size = float(op["size"])
            item.SetTextSize(pcbnew.VECTOR2I(iu(size), iu(size)))
        if op.get("thickness") is not None:
            item.SetTextThickness(iu(float(op["thickness"])))
        if op.get("hide") is not None:
            item.SetVisible(not op["hide"])
        sz = item.GetTextSize()
        return {"ref": op["ref"], "field": op["field"],
                "layer": pcbnew.LSET.Name(item.GetLayer()),
                "hidden": not item.IsVisible(),
                "size": round(pcbnew.ToMM(sz.y), 6),
                "thickness": round(pcbnew.ToMM(item.GetTextThickness()), 6)}
    raise KeyError(f"unknown text op '{kind}'")


EDGE_BOX = None   # board-edge bbox as plain ints, cached BEFORE any removal
# Every FOOTPRINT/PCB_SHAPE wrapper touched by a removal stays referenced for
# the life of this (short, single-job) process. Letting SWIG garbage-collect
# them corrupts the board: after one silk_clear returns, the NEXT
# board.FindFootprintByReference() comes back as a bare SwigPyObject with no
# FOOTPRINT methods (KiCad 10.0.5, measured - and it is the wrappers going out
# of scope, not `thisown`, which is False on both sides).
_KEEPALIVE = []


def _box(bb) -> tuple:
    return (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom())


def _inside(inner: tuple, outer: tuple) -> bool:
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def _grow(b: tuple, d: int) -> tuple:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _touches(a: tuple, b: tuple) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def apply_silk_clear(board, op: dict) -> dict:
    """{"op": "silk_clear", "ref": R, ["layer": "F.SilkS"],
        ["only_offboard": true]}

    Delete a footprint's own GRAPHIC silk items (lines, arcs, circles, polys).
    Reference/Value text is never touched - move those with move_text.

    Why this exists: footprint-INTERNAL silk is the librarian's to fix in the
    LIBRARY, but a library edit does not reach a board that is already placed,
    and nothing else in this pipeline can edit footprint graphics on a board.
    That mattered on g0-sense P6: 12 of 13 residual silk warnings were body
    outlines (a 0603 wedged between two ICs, and a flush edge connector whose
    mouth-end silk hangs off the board), and `drc_routed` fails at
    errors+warnings = 0, so they had to go before P7 could pass. Re-running
    board_init to pick up a library edit would have destroyed the placement.

    `only_offboard` keeps every item whose bounding box lies inside the board
    outline and deletes the rest - the flush-connector case, where the silk
    that offends is the part hanging past the edge and the rest is worth
    keeping. Without it the footprint's graphic silk goes entirely.

    Idempotent: nothing left to delete is a no-op with {"removed": 0}."""
    ref = op["ref"]
    fp = board.FindFootprintByReference(ref)
    if fp is None:
        raise KeyError(f"footprint '{ref}' not on board")
    lname = op.get("layer", "F.SilkS")
    lid = _layer_id(board, lname)
    keep_inside = bool(op.get("only_offboard"))
    region = op.get("region")
    if region is not None:
        region = (iu(region[0]), iu(region[1]), iu(region[2]), iu(region[3]))
    over_pads = bool(op.get("over_pads"))
    edge_mm = op.get("edge_mm")
    at = op.get("at")
    if (keep_inside or edge_mm is not None) and EDGE_BOX is None:
        raise RuntimeError("only_offboard/edge_mm need the board-edge box "
                           "cached before the first removal - see main()")
    inner = None
    if edge_mm is not None:
        m = iu(edge_mm)
        inner = (EDGE_BOX[0] + m, EDGE_BOX[1] + m,
                 EDGE_BOX[2] - m, EDGE_BOX[3] - m)
    pad_boxes = []
    if over_pads:   # silk_clear.py: a segment across a pad's mask opening
        pad_boxes = [_box(p.GetBoundingBox()) for p in fp.Pads()]
    grow_pad, grow_at = iu(0.1), iu(0.05)
    if at is not None:
        at = (iu(at[0]), iu(at[1]))
    uuids = set(op["uuid"]) if op.get("uuid") is not None else None
    victims = []
    for item in list(fp.GraphicalItems()):
        if item.GetLayer() != lid:
            continue
        if isinstance(item, pcbnew.PCB_TEXT):
            continue          # ${REFERENCE}-style fields are move_text's job
        if keep_inside and _inside(_box(item.GetBoundingBox()), EDGE_BOX):
            continue
        if region is not None \
                and not _inside(_box(item.GetBoundingBox()), region):
            continue          # silk_rm.py: one end of a body outline only
        if uuids is not None and item.m_Uuid.AsString() not in uuids:
            continue
        bb = _box(item.GetBoundingBox())
        if inner is not None and _inside(bb, inner):
            continue          # silk_edge.py: clear of the edge band
        if over_pads and not any(_touches(_grow(bb, grow_pad), pb)
                                 for pb in pad_boxes):
            continue
        if at is not None:
            g = _grow(bb, grow_at)
            if not (g[0] <= at[0] <= g[2] and g[1] <= at[1] <= g[3]):
                continue
            victims.append(item)
            break             # silk_clean.py: the one item DRC pointed at
        victims.append(item)
    _KEEPALIVE.append(fp)
    _KEEPALIVE.extend(victims)
    for item in victims:
        fp.Remove(item)
    return {"ref": ref, "layer": lname, "removed": len(victims),
            "only_offboard": keep_inside, "region": op.get("region")}


# set_pad zone_connection word -> pcbnew enum name. "inherit" clears the
# pad's own setting so the footprint's (then the zone's) applies again.
ZONE_CONNECTIONS = {"inherit": "ZONE_CONNECTION_INHERITED",
                    "none": "ZONE_CONNECTION_NONE",
                    "thermal": "ZONE_CONNECTION_THERMAL",
                    "solid": "ZONE_CONNECTION_FULL"}


def apply_set_pad(board, op: dict) -> dict:
    fp = board.FindFootprintByReference(op["ref"])
    if fp is None:
        raise KeyError(f"footprint '{op['ref']}' not on board")
    nums = [op["pad"]] if isinstance(op["pad"], str) else list(op["pad"])
    pads = [p for p in fp.Pads() if p.GetNumber() in nums]
    missing = set(nums) - {p.GetNumber() for p in pads}
    if missing:
        raise KeyError(f"{op['ref']}: no pad {sorted(missing)}")
    net = None
    if op.get("net"):
        net = board.FindNet(op["net"])
        if net is None:
            net = pcbnew.NETINFO_ITEM(board, op["net"])
            board.Add(net)
    size = op.get("size")
    if size is not None and not isinstance(size, list):
        size = [size, size]
    zc = op.get("zone_connection")
    if zc is not None:
        zc = getattr(pcbnew, ZONE_CONNECTIONS[zc])
    for p in pads:
        if "net" in op:
            if net is None:
                p.SetNetCode(0)
            else:
                p.SetNet(net)
        if size is not None:
            p.SetSize(pcbnew.VECTOR2I(iu(size[0]), iu(size[1])))
        if zc is not None:
            p.SetLocalZoneConnection(zc)
    return {"ref": op["ref"], "pads": sorted(p.GetNumber() for p in pads),
            "net": op.get("net"), "size": size,
            "zone_connection": op.get("zone_connection")}


# swap_fp: (old part, new part, uuid to carry) - the old parts are removed,
# and their uuids given to the new ones, after the last op, before Save
_DOOMED = []
REPAD_TOL = 0.005  # mm, = place_edit.REPAD_TOL
NATIVE_FIELDS = ("Reference", "Value", "Footprint")


def _pin_data(fp) -> dict:
    """{pad number: (pin function, pin type)} of the first pad numbered so."""
    pins = {}
    for p in fp.Pads():
        pins.setdefault(p.GetNumber(), (p.GetPinFunction(), p.GetPinType()))
    return pins


def _set_pad_net(board, pad, net: str, cache: dict) -> None:
    if net:
        pad.SetNet(update_swig.net_of(board, net, cache))
    else:
        pad.SetNetCode(0)


def apply_swap_fp(board, op: dict) -> dict:
    ref = op["ref"]
    old = board.FindFootprintByReference(ref)
    if old is None:
        raise KeyError(f"footprint '{ref}' not on board")
    new = pcbnew.FootprintLoad(op["_lib"], op["_name"])
    if new is None:
        raise KeyError(f"swap_fp {ref}: cannot load footprint "
                       f"{op['_name']} from {op['_lib']}")
    lib, name = op["_fpid"].split(":", 1)
    new.SetFPID(pcbnew.LIB_ID(lib, name))
    pins = _pin_data(old)
    custom = {f.GetName(): f.GetText() for f in old.GetFields()
              if f.GetName() not in NATIVE_FIELDS}
    old.SetReference(ref + "__swap_old")  # later ops on REF find the new one
    new.SetReference(ref)
    update_swig.apply_fields(new, old.GetValue(), custom)
    board.Add(new)
    _KEEPALIVE.extend([old, new])
    # where the old part sits first, so inherit() lays the field texts where
    # they were; then the pose (move, side, absolute angle: place's order)
    new.SetPosition(old.GetPosition())
    set_side(new, side_of(old))
    new.SetOrientationDegrees(old.GetOrientationDegrees())
    update_swig.inherit(new, old, True)
    pose = op["_pose"]
    new.SetPosition(pcbnew.VECTOR2I(iu(pose["x"]), iu(pose["y"])))
    set_side(new, pose["side"])
    new.SetOrientationDegrees(float(pose["deg"]))
    cache = {}
    nets = op.get("_nets") or {}
    zones = op.get("_zone") or {}
    for p in new.Pads():
        n = p.GetNumber()
        _set_pad_net(board, p, nets.get(n, ""), cache)
        if n in pins:
            p.SetPinFunction(pins[n][0])
            p.SetPinType(pins[n][1])
        if n in zones:
            p.SetLocalZoneConnection(getattr(pcbnew, ZONE_CONNECTIONS[zones[n]]))
    new.SetDNP(old.IsDNP())
    new.SetLocked(bool(op.get("_locked", old.IsLocked())))
    _DOOMED.append((old, new, op.get("_uuid")))
    pos = new.GetPosition()
    return {"ref": ref, "footprint": op["_fpid"],
            "x": round(pcbnew.ToMM(pos.x), 6), "y": round(pcbnew.ToMM(pos.y), 6),
            "deg": round(new.GetOrientationDegrees(), 4),
            "side": side_of(new), "locked": new.IsLocked(),
            "pads": {p.GetNumber(): p.GetNetname() for p in new.Pads()
                     if p.GetNumber()}}


def apply_renumber(board, op: dict) -> dict:
    ref = op["ref"]
    fp = board.FindFootprintByReference(ref)
    if fp is None:
        raise KeyError(f"footprint '{ref}' not on board")
    pads = list(fp.Pads())
    pins = _pin_data(fp)  # before any number changes
    tol = iu(REPAD_TOL)
    plan = []
    for e in op["_pads"]:
        x, y = iu(e["at"][0]), iu(e["at"][1])
        hit = [p for p in pads if math.hypot(p.GetPosition().x - x,
                                             p.GetPosition().y - y) <= tol]
        if len(hit) > 1:
            hit = [p for p in hit if p.IsOnCopperLayer()]
        if len(hit) != 1:
            raise KeyError(f"renumber {ref}: {len(hit)} pads at {e['at']}")
        if any(hit[0] is q for q, _ in plan):
            raise KeyError(f"renumber {ref}: the pad at {e['at']} is named "
                           "twice")
        plan.append((hit[0], e))
    cache = {}
    done = []
    for p, e in plan:
        n = e["number"]
        was = p.GetNumber()
        p.SetNumber(n)
        _set_pad_net(board, p, e.get("net", ""), cache)
        if n in pins:
            p.SetPinFunction(pins[n][0])
            p.SetPinType(pins[n][1])
        if e.get("zone") is not None:
            p.SetLocalZoneConnection(getattr(pcbnew, ZONE_CONNECTIONS[e["zone"]]))
        done.append({"at": e["at"], "was": was, "number": n,
                     "net": p.GetNetname()})
    board.BuildListOfNets()
    return {"ref": ref, "renumbered": done}


def apply_op(board, op: dict) -> dict:
    kind = op["op"]
    if kind == "set_pad":
        return apply_set_pad(board, op)
    if kind == "swap_fp":
        return apply_swap_fp(board, op)
    if kind == "renumber":
        return apply_renumber(board, op)
    if kind == "set_dnp":
        fp = board.FindFootprintByReference(op["ref"])
        if fp is None:
            raise KeyError(f"footprint '{op['ref']}' not on board")
        fp.SetDNP(bool(op["dnp"]))
        return {"ref": op["ref"], "dnp": fp.IsDNP()}
    if kind in ("add_text", "remove_text", "move_text", "set_text"):
        return apply_text_op(board, op)
    if kind == "silk_clear":
        return apply_silk_clear(board, op)
    ref = op["ref"]
    fp = board.FindFootprintByReference(ref)
    if fp is None:
        raise KeyError(f"footprint '{ref}' not on board")
    if kind in ("place", "move"):
        fp.SetPosition(pcbnew.VECTOR2I(iu(op["x"]), iu(op["y"])))
    if kind == "place" and op.get("side") is not None:
        set_side(fp, op["side"])
    if kind in ("place", "rotate") and op.get("deg") is not None:
        fp.SetOrientationDegrees(float(op["deg"]))
    if kind == "flip":
        set_side(fp, op["side"])
    if kind == "lock":
        fp.SetLocked(bool(op["locked"]))
    pos = fp.GetPosition()
    return {"ref": ref,
            "x": round(pcbnew.ToMM(pos.x), 6), "y": round(pcbnew.ToMM(pos.y), 6),
            "deg": round(fp.GetOrientationDegrees(), 4),
            "side": side_of(fp), "locked": fp.IsLocked()}


def main() -> int:
    job = json.loads(open(sys.argv[1], encoding="utf-8").read())
    try:
        with safelib.board_locks(job):  # LoadBoard -> Save under one hold
            return _run(job)
    except safelib.LockBusy as e:  # another writer holds the board
        print(json.dumps({"ok": False, "index": None, "error": str(e)}))
        return 3


def _run(job) -> int:
    global EDGE_BOX
    board = pcbnew.LoadBoard(job["board"])
    # GetBoardEdgesBoundingBox() SEGFAULTS once a footprint has had an item
    # removed (KiCad 10.0.5, measured) - and returning its BOX2I into a later
    # op corrupts the next FindFootprintByReference into a bare SwigPyObject.
    # Take it once, up front, as plain ints.
    if any(o.get("op") == "silk_clear"
           and (o.get("only_offboard") or o.get("edge_mm") is not None)
           for o in job["ops"]):
        EDGE_BOX = _box(board.GetBoardEdgesBoundingBox())
    results = []
    for i, op in enumerate(job["ops"]):
        try:
            results.append(apply_op(board, op))
        except Exception as e:  # no save on ANY failure
            print(json.dumps({"ok": False, "index": i,
                              "error": f"{type(e).__name__}: {e}"}))
            return 3
    try:  # swap_fp's removals last, the uuid once the old part is gone
        for old, new, uuid in _DOOMED:
            board.RemoveNative(old)  # RemoveNative: see apply_text_op
            if uuid:
                new.SetUuid(pcbnew.KIID(uuid))
        if _DOOMED:
            board.BuildListOfNets()
    except Exception as e:  # noqa: BLE001 - no save on any failure
        print(json.dumps({"ok": False, "index": None,
                          "error": f"swap_fp removal: {type(e).__name__}: "
                                   f"{e}"}))
        return 3
    if not board.Save(job["out"]):
        print(json.dumps({"ok": False, "index": None,
                          "error": f"board.Save failed: {job['out']}"}))
        return 3
    # a line of its own, flushed: SWIG's C-level noise (a removal's leak
    # report) may share stdout without a newline
    print("\n" + json.dumps({"ok": True, "results": results}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
