"""route_swig - BUNDLED-python SWIG worker for the S11 routing pipeline.

Runs inside KiCad's bundled python (the only interpreter with pcbnew); invoked
as `python route_swig.py job.json` by the venv drivers (route_edit.py,
route_auto.py, planes_gen.py). stdlib + lib/safelib (itself stdlib) +
lib/teardrop_swig (add_via's teardrops; stdlib at import).
Every job runs under safelib.board_locks(job): an OS writer lock on the
"board" and "out" .kicad_pcb (<pcb>.lock), held from LoadBoard to Save;
a second worker on the same board waits, then refuses (LockBusy).

The RESULT is written to the file named by job["result"], never stdout: bulk
track/via removal sprays C-level "memory leak of type 'PCB_TRACK *'" lines and
wx spews image-handler noise, both of which tear stdout mid-line (prior-attempt
fact). Exit 0 = result file written with {"ok": true}; exit 3 = failure (result
file has {"ok": false, ...} when possible). Nothing is saved on failure.

job JSON: {"verb": ..., "result": out.json, ...} with verbs:

  export_dsn: {"board": pcb, "dsn": out.dsn, "layer_types": {"In1.Cu": "power"},
               "sort_refs": true}
      Specctra DSN export. sort_refs (optional) puts footprints in reference
      order first, so the DSN does not follow the board's file order (#1412). wx asserts suppressed (export wedges headless on a
      courtyard-polygon wxASSERT otherwise). layer_types (optional) sets
      LT_POWER/LT_SIGNAL per layer BEFORE export (DSN "(type power)" drives
      Freerouting's plane rules; auto-detection does not exist) - transient,
      the board file is not saved by this verb.

  import_ses: {"board": pcb, "ses": in.ses, "out": out_pcb}
  retype_vias: {"board": pcb, "spans": [[top, bot, kind], ...], "out": out_pcb}
      Specctra SES import (adds copper only; footprints/zones untouched) then
      Save(out). Save may drop a default .kicad_pro next to `out` - drivers
      stage in a scratch dir and move only the .kicad_pcb back.

  apply_ops: {"board": pcb, "out": out_pcb, "ops": [...]}
      Track/via op list (route_edit.py's payload). Additions are checked for
      an existing identical item first (skip -> idempotent re-application);
      removals are by uuid and happen LAST, after every read and addition
      (bulk Remove() corrupts later pcbnew calls in the same process - the
      save is the final act). Op shapes:
        {"op": "add_track", "start": [x,y], "end": [x,y], "width": mm,
         "layer": "F.Cu", "net": "NAME"}
        {"op": "add_via", "at": [x,y], "size": mm, "drill": mm, "net": "NAME"}
            # optional, a span (#477): "kind": micro|blind|buried, "top", "bot"
            # optional (#1138): "teardrops": {teardroplib.ITEM_PARAMS names}
            #   enables the via's teardrops with those settings; a key left
            #   out takes the default on a new via and keeps its value on a
            #   via already there, whose "exists" result says
            #   "teardrops": "set" when anything changed
            # optional (#1147): "filled": bool sets filling + capping as
            #   set_via_fill does; an "exists" result says "filled": "set"
            #   when that changed the via
        {"op": "remove", "uuid": "..."}        # track, via or zone; absent = no-op
        {"op": "set_net", "uuid": "...", "net": "NAME"}   # renet one item
        {"op": "set_width", "uuid": "...", "width": mm}   # a track or arc
        {"op": "set_via_fill", "uuid": "...", "filled": bool}  # fill + cap
        {"op": "set_point", "uuid": "...", "which": "start"|"end"|"at",
         "to": [x,y]}      # move a track's end or a via's centre in place
        {"op": "rename_net", "from": "OLD", "to": "NEW"}
            # every pad, track, via and zone of OLD goes to NEW (created
            # when absent); OLD stays in the net list, empty
      All coordinates mm; mm->IU via pcbIUScale.mmToIU (FromMM truncates).

  set_rule_areas: {"board": pcb, "out": out_pcb, "prefix": "aiee_keepout",
              "areas": [...]}
      Replaces the rule areas named prefix* (keepout_gen.py); see
      verb_set_rule_areas for an area's shape.

  add_zones: {"board": pcb, "out": out_pcb, "zones": [...],
              "layer_types": {"In1.Cu": "power"}}
      Zone creation for planes_gen.py. Zones are saved UNFILLED - the caller
      refills via `kicad-cli pcb drc --refill-zones --save-board` (ZONE_FILLER
      segfaults headless on this host). layer_types here ARE saved with the
      board. Zone shape:
        {"net": "GND", "layer": "In1.Cu", "poly": [[x,y], ...] |
         "rect": [x1,y1,x2,y2], "priority": 0, "min_island_mm2": null,
         "clearance": mm, "min_width": mm,
         "connect": "solid"|"thermal"|null,   # solid -> (connect_pads yes)
         "name": "PATCH_VBUS"}                # optional (zone_name)
      "remove" (optional): [{"name": "PATCH_VBUS", "layer": "F.Cu",
         "inside": [x1,y1,x2,y2], "net": "VBUS"}] drops the zones of that
         name and layer (and net, when given; "" names an unnamed zone)
      whose bounding box lies inside the box (0.05 mm slack) - after every
      addition, just before the save (zone_patch.py's replace / remove).
"""
import json
import sys

import safelib  # lib/ is sys.path[0]: the worker runs as lib/route_swig.py
import teardrop_swig  # add_via teardrops; pcbnew only inside its calls


def _wx_quiet():
    import wx

    app = wx.App()
    wx.DisableAsserts()
    try:
        app.SetAssertMode(wx.APP_ASSERT_SUPPRESS)
    except Exception:
        pass
    return app  # keep a reference so the App outlives the call


def iu(mm):
    import pcbnew

    return int(pcbnew.pcbIUScale.mmToIU(float(mm)))


def mm(iu_val):
    import pcbnew

    return round(pcbnew.ToMM(int(iu_val)), 6)


def _layer_id(board, name):
    lid = board.GetLayerID(str(name))
    if lid < 0:
        raise ValueError("unknown layer: %s" % name)
    return lid


def _set_fill(pcbnew, via, filled):
    """Filling + capping (IPC-4761 Type VII) on or off; True if it changed
    the via."""
    if filled:
        fm, cm = pcbnew.FILLING_MODE_FILLED, pcbnew.CAPPING_MODE_CAPPED
    else:
        fm = pcbnew.FILLING_MODE_NOT_FILLED
        cm = pcbnew.CAPPING_MODE_NOT_CAPPED
    if via.GetFillingMode() == fm and via.GetCappingMode() == cm:
        return False
    via.SetFillingMode(fm)
    via.SetCappingMode(cm)
    return True


def _net(board, name):
    net = board.FindNet(str(name))
    if net is None:
        raise ValueError("net not on board: %s" % name)
    return net


def _set_layer_types(board, layer_types):
    import pcbnew

    kinds = {"power": pcbnew.LT_POWER, "signal": pcbnew.LT_SIGNAL,
             "mixed": pcbnew.LT_MIXED, "jumper": pcbnew.LT_JUMPER}
    for lname, kind in (layer_types or {}).items():
        if kind not in kinds:
            raise ValueError("layer_types[%s]: unknown type %r" % (lname, kind))
        board.SetLayerType(_layer_id(board, lname), kinds[kind])


def _sort_footprints(board):
    """Put the board's footprints in reference order (transient: the
    export verb never saves). KiCad keeps footprints in file order on load
    and saves them sorted by UUID, and a footprint written without a UUID
    gets a random one each load, so the order is arbitrary. The DSN follows
    it: components, and the name of the image footprints of one shape
    share (the first one met), and Freerouting's result follows the DSN
    (#1412)."""
    fps = sorted(board.Footprints(), key=lambda f: (f.GetReference(),
                                                    str(f.m_Uuid.AsString())))
    for fp in fps:
        board.Remove(fp)
    import pcbnew
    append = pcbnew.ADD_MODE_APPEND  # the default INSERT puts each in front
    for fp in fps:
        fp.thisown = 0
        board.AddNative(fp, append)


def verb_export_dsn(job):
    import pcbnew

    _wx_quiet()
    board = pcbnew.LoadBoard(job["board"])
    _set_layer_types(board, job.get("layer_types"))
    if job.get("sort_refs"):
        _sort_footprints(board)
    ok = pcbnew.ExportSpecctraDSN(board, job["dsn"])
    if not ok:
        raise RuntimeError("ExportSpecctraDSN returned False")
    return {"dsn": job["dsn"]}


def verb_import_ses(job):
    import pcbnew

    _wx_quiet()
    board = pcbnew.LoadBoard(job["board"])
    before = len(board.GetTracks())
    ok = pcbnew.ImportSpecctraSES(board, job["ses"])
    if not ok:
        raise RuntimeError("ImportSpecctraSES returned False")
    after = len(board.GetTracks())
    if not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"tracks_before": before, "tracks_after": after, "out": job["out"]}


def verb_retype_vias(job):
    """KiCad's SES import calls every via that isn't through "buried", even a
    microvia from an outer layer (#477). Give each the type of the build's
    span it runs: job["spans"] is [[top layer, bottom layer, kind], ...]. A
    via on no listed span is left alone and counted."""
    import pcbnew

    _wx_quiet()
    board = pcbnew.LoadBoard(job["board"])
    kinds = {(a, b): k for a, b, k in job["spans"]}
    done, off = 0, 0
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetViaType() == pcbnew.VIATYPE_THROUGH:
            continue
        span = (board.GetLayerName(t.TopLayer()), board.GetLayerName(t.BottomLayer()))
        kind = kinds.get(span)
        if kind is None:
            off += 1
            continue
        want = VIA_TYPES[kind](pcbnew)
        if t.GetViaType() != want:
            t.SetViaType(want)
            done += 1
    if not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"retyped": done, "off_span": off, "out": job["out"]}


def _track_key(t):
    import pcbnew

    s, e = t.GetStart(), t.GetEnd()
    a, b = (s.x, s.y), (e.x, e.y)
    if b < a:
        a, b = b, a
    return (a, b, t.GetWidth(), t.GetLayer(), t.GetNetname())


VIA_TYPES = {"through": lambda p: p.VIATYPE_THROUGH,
             "micro": lambda p: p.VIATYPE_MICROVIA,
             "blind": lambda p: p.VIATYPE_BLIND,
             "buried": lambda p: p.VIATYPE_BURIED}


def verb_apply_ops(job):
    import pcbnew

    board = pcbnew.LoadBoard(job["board"])
    tol = iu(0.001)

    # Read pass: index existing items (before any mutation). An item a
    # remove in this list takes is left out, whatever the order: the
    # removal pass deletes it, so an add matching it (the push rips a via,
    # the route lays one on the same spot) makes a new item, not "exists".
    # A re-run finds that uuid absent and the new item there.
    gone = {op["uuid"] for op in job["ops"] if op["op"] == "remove"}
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"
              and t.m_Uuid.AsString() not in gone]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"
            and t.m_Uuid.AsString() not in gone]
    track_keys = {_track_key(t) for t in tracks}
    via_index = {}
    for v in vias:
        p = v.GetPosition()
        via_index.setdefault((p.x, p.y, v.GetNetname(), v.TopLayer(),
                              v.BottomLayer()), v)
    by_uuid = {}
    for t in board.GetTracks():
        by_uuid[t.m_Uuid.AsString()] = t
    # remove also takes a zone uuid (a superseded plane planes_gen left);
    # kept apart so set_width/set_point/set_net stay track/via only.
    zone_by_uuid = {z.m_Uuid.AsString(): z for z in board.Zones()}

    results = []
    to_remove = []
    drops_taken = set()     # remove_teardrop: zones (load-time uuids) taken
    for i, op in enumerate(job["ops"]):
        kind = op["op"]
        if kind == "add_track":
            start = pcbnew.VECTOR2I(iu(op["start"][0]), iu(op["start"][1]))
            end = pcbnew.VECTOR2I(iu(op["end"][0]), iu(op["end"][1]))
            lid = _layer_id(board, op["layer"])
            net = _net(board, op["net"])
            a, b = (start.x, start.y), (end.x, end.y)
            if b < a:
                a, b = b, a
            key = (a, b, iu(op["width"]), lid, op["net"])
            if key in track_keys:
                results.append({"op": kind, "status": "exists"})
                continue
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(start)
            t.SetEnd(end)
            t.SetWidth(iu(op["width"]))
            t.SetLayer(lid)
            t.SetNet(net)
            board.Add(t)
            track_keys.add(key)
            results.append({"op": kind, "status": "added",
                            "uuid": t.m_Uuid.AsString()})
        elif kind == "add_via":
            at = pcbnew.VECTOR2I(iu(op["at"][0]), iu(op["at"][1]))
            net = _net(board, op["net"])
            # a span (#477): stacked vias share a centre and a net, so the
            # layers are part of what makes a via already there
            top = _layer_id(board, op.get("top", "F.Cu"))
            bot = _layer_id(board, op.get("bot", "B.Cu"))
            hit = None
            for (vx, vy, vn, vt, vb), v in via_index.items():
                if vn == op["net"] and vt == top and vb == bot \
                        and abs(vx - at.x) <= tol and abs(vy - at.y) <= tol:
                    hit = v
                    break
            td = op.get("teardrops")
            if hit is not None:
                res = {"op": kind, "status": "exists"}
                if "filled" in op and _set_fill(pcbnew, hit, op["filled"]):
                    res["filled"] = "set"     # a fill_vias in the list (#1147)
                if td is not None:
                    # merged onto the via's own settings: a key left out
                    # keeps its value, not the default (#1138)
                    was = teardrop_swig.get_params(hit, mm)
                    keep = {k: x for k, x in was.items() if k != "enabled"}
                    teardrop_swig.set_params(hit, dict(keep, **td), iu)
                    if teardrop_swig.get_params(hit, mm) != was:
                        res["teardrops"] = "set"
                results.append(res)
                continue
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(at)
            v.SetWidth(iu(op["size"]))
            v.SetDrill(iu(op["drill"]))
            v.SetViaType(VIA_TYPES[op.get("kind", "through")](pcbnew))
            v.SetLayerPair(top, bot)
            v.SetNet(net)
            if td is not None:
                teardrop_swig.set_params(
                    v, dict(teardrop_swig.ITEM_DEFAULTS, **td), iu)
            if "filled" in op:
                _set_fill(pcbnew, v, op["filled"])
            board.Add(v)
            via_index[(at.x, at.y, op["net"], top, bot)] = v
            results.append({"op": kind, "status": "added",
                            "uuid": v.m_Uuid.AsString()})
        elif kind == "set_net":
            item = by_uuid.get(op["uuid"])
            if item is None:
                raise ValueError("ops[%d]: uuid %s not on board"
                                 % (i, op["uuid"]))
            if item.GetNetname() == op["net"]:
                results.append({"op": kind, "status": "exists"})
            else:
                item.SetNet(_net(board, op["net"]))
                results.append({"op": kind, "status": "renetted"})
        elif kind in ("set_width", "set_point"):
            item = by_uuid.get(op["uuid"])
            if item is None:
                raise ValueError("ops[%d]: uuid %s not on board"
                                 % (i, op["uuid"]))
            is_via = item.GetClass() == "PCB_VIA"
            if kind == "set_width":
                if is_via:
                    raise ValueError("ops[%d]: set_width on a via" % i)
                same = abs(item.GetWidth() - iu(op["width"])) <= 1
                if not same:
                    item.SetWidth(iu(op["width"]))
            else:
                to = pcbnew.VECTOR2I(iu(op["to"][0]), iu(op["to"][1]))
                if (op["which"] == "at") != is_via:
                    raise ValueError("ops[%d]: set_point %s on a %s"
                                     % (i, op["which"], item.GetClass()))
                get, put = {"at": ("GetPosition", "SetPosition"),
                            "start": ("GetStart", "SetStart"),
                            "end": ("GetEnd", "SetEnd")}[op["which"]]
                cur = getattr(item, get)()
                same = abs(cur.x - to.x) <= tol and abs(cur.y - to.y) <= tol
                if not same:
                    getattr(item, put)(to)
            results.append({"op": kind, "status": "exists" if same
                            else "changed"})
        elif kind == "set_via_fill":
            item = by_uuid.get(op["uuid"])
            if item is None or item.GetClass() != "PCB_VIA":
                raise ValueError("ops[%d]: via %s not on board"
                                 % (i, op["uuid"]))
            same = not _set_fill(pcbnew, item, op["filled"])
            results.append({"op": kind, "status": "exists" if same
                            else "changed"})
        elif kind == "rename_net":
            new = board.FindNet(str(op["to"]))
            if new is None:
                new = pcbnew.NETINFO_ITEM(board, str(op["to"]))
                board.Add(new)
            items = [p for fp in board.GetFootprints() for p in fp.Pads()]
            items += list(board.GetTracks()) + list(board.Zones())
            n = 0
            for it in items:
                if it.GetNetname() == op["from"]:
                    it.SetNet(new)
                    n += 1
            results.append({"op": kind, "status": "renamed" if n else
                            "absent", "count": n})
        elif kind == "remove_teardrop":
            # a teardrop zone has no uuid in the file (#1144): the driver
            # names it by net, layer and one vertex of its outline
            at = (iu(op["at"][0]), iu(op["at"][1]))
            lid = _layer_id(board, op["layer"])
            hit = None
            for z in board.Zones():
                if not z.IsTeardropArea() or z.GetNetname() != op["net"] \
                        or z.GetLayer() != lid \
                        or z.m_Uuid.AsString() in drops_taken:
                    continue
                o = z.Outline()
                for k in range(o.TotalVertices()):
                    v = o.CVertex(k)
                    if abs(v.x - at[0]) <= tol and abs(v.y - at[1]) <= tol:
                        hit = z
                        break
                if hit is not None:
                    break
            if hit is None:
                results.append({"op": kind, "status": "absent"})
            else:
                drops_taken.add(hit.m_Uuid.AsString())
                to_remove.append(hit)
                results.append({"op": kind, "status": "removed"})
        elif kind == "remove":
            item = by_uuid.get(op["uuid"]) or zone_by_uuid.get(op["uuid"])
            if item is None:
                results.append({"op": kind, "status": "absent"})
            else:
                to_remove.append(item)
                results.append({"op": kind, "status": "removed"})
        else:
            raise ValueError("ops[%d]: unknown op %r" % (i, kind))

    # Removal pass LAST; Save immediately after; no pcbnew reads beyond this.
    for item in to_remove:
        board.Remove(item)
    if not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"results": results, "removed": len(to_remove)}


def verb_add_zones(job):
    import pcbnew

    board = pcbnew.LoadBoard(job["board"])
    _set_layer_types(board, job.get("layer_types"))
    made = []
    fresh = set()
    for i, z in enumerate(job["zones"]):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(_layer_id(board, z["layer"]))
        zone.SetNet(_net(board, z["net"]))
        zone.SetAssignedPriority(int(z.get("priority", 0)))
        if z.get("clearance") is not None:
            zone.SetLocalClearance(iu(z["clearance"]))
        if z.get("min_width") is not None:
            zone.SetMinThickness(iu(z["min_width"]))
        if z.get("min_island_mm2") is not None:
            zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_AREA)
            zone.SetMinIslandArea(int(float(z["min_island_mm2"]) * 1e12))
        if z.get("connect") == "solid":
            # (connect_pads yes ...) - solid pad connection for fan-in
            # lobes (T6 P7B-2; ZONE_CONNECTION::FULL == 2 in KiCad 10)
            zone.SetPadConnection(
                getattr(pcbnew, "ZONE_CONNECTION_FULL", 2))
        pts = z.get("poly")
        if pts is None:
            x1, y1, x2, y2 = z["rect"]
            pts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in pts:
            outline.Append(iu(x), iu(y))
        if z.get("name"):
            zone.SetZoneName(str(z["name"]))
        zone.SetIsFilled(False)
        board.Add(zone)
        fresh.add(zone.m_Uuid.AsString())
        made.append({"index": i, "net": z["net"], "layer": z["layer"],
                     "points": len(pts), "uuid": zone.m_Uuid.AsString()})
    # removals LAST (see apply_ops), never the zones this job just added
    gone, gone_ids = [], set()
    for r in job.get("remove") or []:
        lid = _layer_id(board, r["layer"])
        x1, y1, x2, y2 = r["inside"]
        slack = iu(0.05)
        for zone in board.Zones():
            u = zone.m_Uuid.AsString()
            if u in fresh or u in gone_ids \
                    or zone.GetZoneName() != r["name"] \
                    or (r.get("net") is not None
                        and zone.GetNetname() != r["net"]) \
                    or not zone.IsOnLayer(lid):
                continue
            bb = zone.GetBoundingBox()
            if bb.GetX() >= iu(x1) - slack and bb.GetRight() <= iu(x2) + slack \
                    and bb.GetY() >= iu(y1) - slack \
                    and bb.GetBottom() <= iu(y2) + slack:
                gone.append(zone)
                gone_ids.add(u)
    removed = [{"name": zone.GetZoneName(),
                "uuid": zone.m_Uuid.AsString()} for zone in gone]
    for zone in gone:
        board.Remove(zone)
    if not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"zones": made, "removed": removed, "out": job["out"]}


def verb_set_rule_areas(job):
    """Replace every rule area whose name starts with job["prefix"] by
    job["areas"]: [{"name", "layers": [...], "poly": [[x, y], ...] |
    "rect": [x1, y1, x2, y2], "tracks"/"vias"/"pours"/"pads"/"footprints":
    bool (True = not allowed)}]. New areas are added first and the old ones
    removed last, then one Save (LEARNINGS [swig]: Remove before other
    pcbnew calls corrupts them)."""
    import pcbnew

    board = pcbnew.LoadBoard(job["board"])
    prefix = job["prefix"]
    old = [z for z in board.Zones()
           if z.GetIsRuleArea() and z.GetZoneName().startswith(prefix)]
    made = []
    for a in job["areas"]:
        z = pcbnew.ZONE(board)
        z.SetIsRuleArea(True)
        z.SetZoneName(str(a["name"]))
        ls = pcbnew.LSET()
        for name in a["layers"]:
            ls.AddLayer(_layer_id(board, name))
        z.SetLayerSet(ls)
        z.SetDoNotAllowTracks(bool(a.get("tracks", True)))
        z.SetDoNotAllowVias(bool(a.get("vias", True)))
        z.SetDoNotAllowZoneFills(bool(a.get("pours", True)))
        z.SetDoNotAllowPads(bool(a.get("pads", False)))
        z.SetDoNotAllowFootprints(bool(a.get("footprints", False)))
        pts = a.get("poly")
        if pts is None:
            x1, y1, x2, y2 = a["rect"]
            pts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        o = z.Outline()
        o.NewOutline()
        for x, y in pts:
            o.Append(iu(x), iu(y))
        board.Add(z)
        made.append({"name": a["name"], "layers": list(a["layers"]),
                     "points": len(pts)})
    for z in old:
        board.Remove(z)
    if not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"areas": made, "removed": len(old), "out": job["out"]}


def verb_dedup_copper(job):
    """Remove EXACT-duplicate segments/vias (same geometry+net+layer+width).

    S14 finding: Freerouting's SES echoes pre-session guide-wire copper
    (route_critical trunks) back through ImportSpecctraSES, silently
    duplicating it - same-net exact stacks are invisible to DRC and gerber
    checks (run (a) shipped 45 echoed segments). Duplicates are never
    legitimate copper. Collect-first/remove-last/save-once per the
    LEARNINGS [swig] bulk-Remove rule.
    {"board": pcb, "out": out_pcb}
    """
    import pcbnew

    _wx_quiet()
    board = pcbnew.LoadBoard(job["board"])
    seen = set()
    dups = []
    for t in list(board.GetTracks()):
        cls = t.GetClass()
        if cls == "PCB_VIA":
            p = t.GetPosition()
            key = ("via", p.x, p.y, t.GetWidth(), t.GetDrillValue(),
                   t.GetNetname())
        elif cls == "PCB_TRACK":
            key = ("seg",) + _track_key(t)
        else:                      # PCB_ARC etc.: never emitted duplicated
            continue
        if key in seen:
            dups.append(t)
        else:
            seen.add(key)
    for t in dups:
        board.Remove(t)
    if dups and not board.Save(job["out"]):
        raise RuntimeError("board.Save failed: %s" % job["out"])
    return {"removed": len(dups), "changed": bool(dups),
            "out": job["out"] if dups else job["board"]}


VERBS = {
    "export_dsn": verb_export_dsn,
    "import_ses": verb_import_ses,
    "retype_vias": verb_retype_vias,
    "apply_ops": verb_apply_ops,
    "add_zones": verb_add_zones,
    "dedup_copper": verb_dedup_copper,
    "set_rule_areas": verb_set_rule_areas,
}


def main():
    job = json.loads(open(sys.argv[1], encoding="utf-8").read())
    result_path = job["result"]
    try:
        verb = job.get("verb")
        if verb not in VERBS:
            raise ValueError("unknown verb: %r" % verb)
        payload = {"ok": True, "verb": verb}
        with safelib.board_locks(job):  # LoadBoard -> Save under one hold
            payload.update(VERBS[verb](job))
        rc = 0
    except Exception as e:  # noqa: BLE001
        import traceback

        payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                   "traceback": traceback.format_exc()[-2000:]}
        rc = 3
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return rc


if __name__ == "__main__":
    sys.exit(main())
