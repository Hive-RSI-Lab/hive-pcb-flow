"""board_update - apply a netlist diff to a placed/routed board, keeping copper (T8).

Kills OI-3 (LEARNINGS 2026-07-30 [pipeline]: adding ANY part after P5 used to
cost all of P6+P7). The NEW netlist is the truth; the board is updated to match
it while PRESERVING every piece of copper that still serves a surviving part.

Modes (classified per ref by diffing the netlist against the board; one
invocation applies the whole diff atomically):
  swap_part_same_fp  same footprint, changed value/fields -> field surgery
                     only; geometry (positions, pads, tracks, vias, zones) is
                     untouched. BOM/CPL become stale (state edit class).
  add_part           in netlist, not on board -> footprint inserted at a
                     DECLARED placement (exact x/y or a search region), pads
                     netted from the netlist (the ratsnest stub) - routing is
                     the standard fix loop's job.
  del_part           on board, not in netlist -> footprint removed PLUS its
                     now-orphaned copper: stubs/vias that no longer reach any
                     surviving pad, judged by CONNECTIVITY (lib/netconn - the
                     V13-corrected copper-touch semantics), never proximity.
                     Board-frame silk text inside the removed part's bbox goes
                     with it. Pre-existing dangling copper is preserved and
                     reported (not this edit's to fix).
  swap_part_new_fp   footprint CHANGED on a surviving ref. At its old spot
                     (the default; --placements moves it) a PIN-COMPATIBLE
                     package keeps the part's routing: every old pad copper
                     touches has a same-number new pad, on the same net,
                     that the same copper still touches (an HTSSOP gaining
                     an exposed pad, 0603 -> 0805). Copper of other nets the
                     new pads would short or crowd under the clearance is
                     ripped, with what it alone held up (the fix loop
                     reroutes those nets). The clearance is weighed per
                     pair: a .kicad_dru rule held to a footprint or a net
                     name counts only where it may hold on the board the
                     update leaves, and copper the old pad already came as
                     close to under the same rules stays (the plan's notes
                     list it; DRC still flags it) (#1146). The new
                     footprint inherits the old one's schematic link,
                     BOM/pos exclusions and field texts. Otherwise (or
                     --no-keep-copper) it is composed as del_part +
                     add_part and the plan's notes say why.
                     PR #68 (hifi-receiver-2 core: TPA3220 to its EP
                     package) did this with swap_fp.py + rip_ep.py.
  DNP                the netlist's `(property (name "dnp"))` is the truth:
                     a surviving part whose board DNP differs gets it in
                     the swap_part_same_fp field surgery; adds and swaps
                     take it from the netlist (set_dnp.py did this by hand).

  rewire_pad         a surviving part (same footprint) whose pad nets changed,
                     where NO track or via touches any changed pad (an
                     unplaced/unrouted board, or pads not yet routed) ->
                     the pads are re-netted; no copper is touched, zones are
                     refilled. Routing the new nets is the fix loop's job.

A pad-net change on a pad that copper DOES touch (a routed rewire / net
rename) is UNSUPPORTED here - re-netting the pad would leave that copper on
the wrong net; that is reroute territory. --dry-run reports it (exit 1);
apply refuses (exit 2) rather than half-applying a diff.

Atomic + rollback (place_edit pattern): the board + project sidecars are
staged in a scratch dir inside the board's directory; the SWIG worker
(lib/update_swig.py) mutates the copy; the driver independently re-parses and
verifies every mode's outcome (fields landed, adds placed+netted, dels gone,
orphans gone, untouched copper byte-for-byte equivalent, full pad->net parity
vs the netlist, no NEW dangling copper per netconn); zones are refilled
(kicad-cli) and DRC runs on the staged copy; only then does os.replace() swap
the .kicad_pcb in. Any failure leaves the original board byte-identical.

DRC is informational for add_part (new pads legitimately report unconnected
until the fix loop routes them) but a HARD gate for dangling: if the staged
board carries more track_dangling/via_dangling than the original, the update
rolls back - orphan surgery failed.

--state <state.json>: records the applied classes via state.py apply_edit
(T7 invalidation map) so gates/artifacts go stale honestly; the report carries
the resulting human_hold weight. The file is load-validated BEFORE mutation;
after a successful apply a state problem degrades to a loud warning (the
board IS updated - reporting failure would lie).

Known limitations (adversarial-review verdicts, accepted with reasons):
 - a swap_part_new_fp that is not pin-compatible is del+add: the old
   package's stubs are ripped even where a new pad would land on one, and
   board-only custom fields are not carried over (the netlist is the
   truth; the fix loop reconnects).
 - The DRC dangling gate compares unrefilled-before vs refilled-after;
   assert_fresh screens stale fills at apply entry, so the delta is honest
   on pipeline-produced boards.
 - verify_apply's inventory covers tracks+vias (the worker never touches
   zones; a zone regression would surface at refill/DRC, not silently).
 - Region search is front-side only and conservatively treats
   opposite-side courtyards and to-be-deleted parts' copper as obstacles.
 - A swap's clash clearance (swap_clash_details, #1146) takes the largest
   rule that may hold for a pair, not KiCad's last matching one, and
   counts a rule it cannot answer for a pair as holding (a test it does
   not read; a part the update adds or swaps with no spot yet, as a dry
   run's adds; a courtyard it cannot build as KiCad does): both can only
   rip more. DRC on the staged board stays the truth. It weighs only the
   rules route_critical.parse_dru_rules reads: a barrier rule (aiee_iso_*)
   or a min it does not evaluate (a sum such as 0.1mm + 0.05mm) is left
   out, where KiCad holds it, and a file KiCad drops whole for a value it
   rejects holds none, in KiCad as here (#1273).

CLI (SPEC section 6):
  board_update.py --pcb B.kicad_pcb --netlist NEW.net
                  [--placements p.json] [--lib DIR]... [--dry-run]
                  [--no-keep-copper]
                  [--state state.json] [--out-report r.json]
  placements JSON: {"REF": {"x": mm, "y": mm, "deg": 0, "side": "front"}
                    | {"region": [x1, y1, x2, y2], "deg": 0}}
Exit 0 applied (or clean dry-run) / 1 dry-run with unsupported changes /
2 error (nothing applied).
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import sexpdata
from shapely import affinity
from shapely.geometry import Point, box
from shapely.prepared import prep

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import board_init  # noqa: E402  (parse_netlist - the P5 netlist reader)
import checklib  # noqa: E402
import env  # noqa: E402
import fplib  # noqa: E402
import geom  # noqa: E402
import kc  # noqa: E402
import netconn  # noqa: E402
import placelib  # noqa: E402
import routelib  # noqa: E402
from checklib import CheckError  # noqa: E402

WORKER = SCRIPTS / "lib" / "update_swig.py"
POS_TOL = 1e-3   # mm
ANG_TOL = 0.05   # deg
SCAN_STEP = 0.5  # mm, region candidate grid
COURT_CLEAR = 0.2  # mm clearance demanded around a region-scan candidate
# rounds of DRC-led follow-up a clash rip gets (_ripped_danglers)
DANGLE_ROUNDS = 3
# Pad-to-foreign-copper margin the region scan demands: covers every JLC
# clearance floor (0.1016-0.1524 mm) so a "clear" candidate does not land
# 0.05 mm from a pour (machine-hit on the pd-trigger add smoke). A stricter
# board-specific DRU rule can still fire - DRC remains the truth; the scan
# is a heuristic.
PAD_CLEAR = 0.2
NATIVE_PROPS = {"Reference", "Value", "Footprint", "Datasheet", "Description"}

MODE_CLASSES = {  # plan bucket -> invalidation.yaml edit class
    "swap_same_fp": "swap_part_same_fp",
    "swap_new_fp": "swap_part_new_fp",
    "add": "add_part",
    "del": "del_part",
    "rewire": "rewire_pad",
}


def _nets_equal(a: str | None, b: str | None) -> bool:
    """Net-name equality over the no-connect equivalence class: an
    unconnected-* singleton (name embeds pin coordinates that churn across
    exports), an absent netlist pin (hierarchical exports DROP NC singleton
    nets entirely - LEARNINGS row 68) and a netless board pad all denote the
    same thing, a deliberately open pad. Collapsing the class is safe: two
    formerly-NC pads that become REALLY connected get a real net name, which
    never normalizes to ''."""
    def norm(n: str | None) -> str:
        n = n or ""
        return "" if n.startswith("unconnected-") else n
    return norm(a) == norm(b)


# ---------------------------------------------------------------- board side

def _board_fields(pcb: Path) -> dict[str, dict]:
    """{ref: {"value": str, "fields": {name: value}}} from footprint
    (property ...) nodes. Custom fields only (native props excluded)."""
    tree = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for fp in geom._kids(tree, "footprint"):
        ref = None
        value = ""
        fields: dict[str, str] = {}
        for prop in geom._kids(fp, "property"):
            s = geom._strs(prop)
            if len(s) < 1:
                continue
            name = s[0]
            val = s[1] if len(s) > 1 else ""
            if name == "Reference":
                ref = val
            elif name == "Value":
                value = val
            elif name not in NATIVE_PROPS:
                fields[name] = val
        if ref:
            out[ref] = {"value": value, "fields": fields}
    return out


# ------------------------------------------------------------------- diffing

def build_plan(model: placelib.PlaceModel, board_fields: dict,
               comps: list[dict], netmap: dict[str, str]) -> dict:
    """Classify netlist-vs-board differences into the T8 modes."""
    plan = {"swap_same_fp": [], "swap_new_fp": [], "add": [], "del": [],
            "rewire": [], "unsupported": [], "notes": []}
    by_ref = {c["ref"]: c for c in comps}
    board_fps = {ref: f for ref, f in model.footprints.items()
                 if "board_only" not in f.attrs}
    skipped = sorted(set(model.footprints) - set(board_fps))
    if skipped:
        plan["notes"].append(
            f"board_only footprints ignored: {', '.join(skipped)}")

    def pads_of_ref(ref: str) -> dict[str, str]:
        want = {}
        for key, net in netmap.items():
            r, num = key.split(".", 1)
            if r == ref:
                want[num] = net
        return want

    for ref in sorted(set(board_fps) - set(by_ref)):
        f = board_fps[ref]
        nets = sorted({p.net for p in f.pads if p.net})
        plan["del"].append({"ref": ref, "fp": f.fpid,
                            "pos": [round(f.pos[0], 4), round(f.pos[1], 4)],
                            "side": f.side, "nets": nets})

    for ref in sorted(set(by_ref) - set(board_fps)):
        c = by_ref[ref]
        if ref in model.footprints:  # collides with a board_only footprint
            plan["unsupported"].append({
                "ref": ref, "kind": "board_only_collision",
                "msg": f"netlist ref {ref} collides with a board_only "
                       f"footprint (mounting-hole class) - rename one side"})
            continue
        plan["add"].append({"ref": ref, "fp": c["fp"], "value": c["value"],
                            "fields": c["fields"], "pads": pads_of_ref(ref),
                            "pins": c.get("pins") or {},
                            "dnp": bool(c.get("dnp"))})

    for ref in sorted(set(board_fps) & set(by_ref)):
        f, c = board_fps[ref], by_ref[ref]
        fp_changed = f.fpid != c["fp"]
        if fp_changed and f.fpid.lower() == (c["fp"] or "").lower():
            # case-only fpid drift is electrically null - a swap_new_fp
            # here would rip and re-route the part for a no-op change
            plan["notes"].append(
                f"{ref}: footprint id differs only by case ({f.fpid!r} vs "
                f"{c['fp']!r}) - treated as the same footprint")
            fp_changed = False
        if fp_changed:
            plan["swap_new_fp"].append({
                "ref": ref, "fp_old": f.fpid, "fp_new": c["fp"],
                "value": c["value"], "fields": c["fields"],
                "pads": pads_of_ref(ref), "dnp": bool(c.get("dnp")),
                "pins": c.get("pins") or {},
                "pos": [round(f.pos[0], 4), round(f.pos[1], 4)],
                "deg": f.angle, "side": f.side,
                "nets_old": sorted({p.net for p in f.pads if p.net})})
            continue
        want = pads_of_ref(ref)
        mismatches = []
        seen_nums = set()
        for p in f.pads:
            if not p.number:
                continue  # unnumbered mechanical pad, never in a netlist
            seen_nums.add(p.number)
            if not _nets_equal(p.net, want.get(p.number, "")):
                mismatches.append({"pad": p.number, "board": p.net or "",
                                   "netlist": want.get(p.number, "")})
        for num in sorted(set(want) - seen_nums):
            mismatches.append({"pad": num, "board": None,
                               "netlist": want[num]})
        if mismatches:
            missing = [m["pad"] for m in mismatches if m["board"] is None]
            routed = _routed_pads(model.bg, ref,
                                  {m["pad"] for m in mismatches})
            if missing or routed:
                why = (f"netlist pin(s) {missing} have no pad on the board"
                       if missing else
                       f"copper touches pad(s) {routed} - re-netting would "
                       "strand it on the old net")
                plan["unsupported"].append({
                    "ref": ref, "kind": "pad_net_change", "pads": mismatches,
                    "routed_pads": routed,
                    "msg": f"surviving part's pad nets differ and {why} - "
                           "reroute/rename territory, not a board_update "
                           "mode"})
                continue
            plan["rewire"].append({
                "ref": ref, "fp": f.fpid, "changes": mismatches,
                "pads": {m["pad"]: m["netlist"] for m in mismatches}})
            # a rewired part may ALSO have field changes - fall through
        bf = board_fields.get(ref, {"value": "", "fields": {}})
        field_changes = {}
        for name in sorted(c["fields"]):
            old = bf["fields"].get(name, "")
            if old != c["fields"][name]:
                field_changes[name] = [old, c["fields"][name]]
        value_change = ([bf["value"], c["value"]]
                        if bf["value"] != c["value"] else None)
        # the schematic's DNP is the netlist's `(property (name "dnp"))`;
        # the board's is (attr ... dnp) - set_dnp.py synced them by hand
        on_board = "dnp" in f.attrs
        dnp_change = ([on_board, bool(c.get("dnp"))]
                      if on_board != bool(c.get("dnp")) else None)
        extra = sorted(set(bf["fields"]) - set(c["fields"]))
        if value_change or field_changes or dnp_change:
            entry = {"ref": ref, "fp": f.fpid, "value": value_change,
                     "fields": field_changes}
            if dnp_change:
                entry["dnp"] = dnp_change
            if extra:
                entry["board_extra_fields"] = extra
            plan["swap_same_fp"].append(entry)
        elif extra:
            # a field REMOVED from the netlist with nothing else changed:
            # not applied (SWIG field deletion is not worth the risk for
            # metadata) but it must not pass silently - stale LCSC/MPN
            # fields feed the BOM. Only fields in the NETLIST's own field
            # vocabulary count: board-side-only names (e.g. easyeda's
            # "LCSC Part") were never netlist-carried and would note on
            # every part of every board.
            vocab = {name for comp in comps for name in comp["fields"]}
            live = [e for e in extra if e in vocab]
            if live:
                plan["notes"].append(
                    f"{ref}: board carries field(s) absent from the netlist "
                    f"({', '.join(live)}) - left in place; clear them by "
                    "hand if the removal was intentional")
    return plan


def _routed_pads(bg, ref: str, numbers: set[str]) -> list[str]:
    """Which of REF's pads `numbers` does a track or via touch (any net,
    copper-touch on a shared layer)? Zone fills do not count: they are
    refilled after the apply and follow the pad's new net."""
    out = []
    pads = [p for p in bg.pads_of(ref=ref) if p.number in numbers]
    tracks = bg.tracks_of()
    vias = bg.vias_of()
    for p in pads:
        hit = any(t.layer in p.layers and t.poly.intersects(p.poly)
                  for t in tracks) or \
            any(any(v.spans(ly) for ly in p.layers)
                and v.poly.intersects(p.poly) for v in vias)
        if hit:
            out.append(p.number)
    return sorted(set(out))


# ------------------------------------------------- copper-keeping fp swap

def placed_pads(bg, mod_path: Path, ref: str, place: dict,
                nets: dict[str, str]) -> list:
    """The copper pads a library footprint gets once placed at `place`
    ({x, y, deg, side}), as geom.Pad in board coordinates with the
    netlist's nets - what the board will hold after the swap, known before
    it is applied. A back-side part is the library part flipped TOP_BOTTOM
    (update_swig's set_side): local y and pad angle negate, F.* <-> B.*,
    then the same transform geom reads a saved board with (checked against
    KiCad 10's own placement on front and back, 0/30/-90/180 deg, SMD, TH
    and offset-drill pads: test_placed_pads_match_kicad)."""
    root = sexpdata.loads(Path(mod_path).read_text(encoding="utf-8"))
    sink = copy.copy(bg)
    sink._pads, sink.npth = [], []
    back = place.get("side") == "back"
    deg = float(place.get("deg") or 0.0)

    def name(t) -> str:
        return str(t.value() if isinstance(t, sexpdata.Symbol) else t)

    for pad in geom._kids(root, "pad"):
        pad = copy.deepcopy(pad)
        at = geom._kid(pad, "at")
        if at is None:
            continue
        n = geom._nums(at)
        lx, ly, a = n[0], n[1], (n[2] if len(n) > 2 else 0.0)
        ln = geom._kid(pad, "layers")
        if ln is not None:
            ln[1:] = [name(t) for t in ln[1:]]
        if back:
            ly, a = -ly, -a
            if ln is not None:
                ln[1:] = [("B." + t[2:]) if t.startswith("F.") else
                          ("F." + t[2:]) if t.startswith("B.") else t
                          for t in ln[1:]]
            d = geom._kid(pad, "drill")
            o = geom._kid(d, "offset") if d is not None else None
            if o is not None and len(o) > 2:
                o[2] = -o[2]
        at[1:] = [lx, ly, a + deg]
        geom.BoardGeom._add_pad(sink, pad, float(place["x"]),
                                float(place["y"]), deg, ref)
    return [dataclasses.replace(p, net=nets.get(p.number) or None)
            for p in sink._pads]


def _placed_footprint(ref: str, fpid: str, mod_path: Path | None,
                      place: dict):
    """The placelib.Footprint a .kicad_dru footprint test reads of a
    library footprint once placed at `place` ({x, y, deg, side}), as
    placed_pads places its pads: library id, side and both sides'
    courtyards (Footprint.courtyard_exact), flipped TOP_BOTTOM for the
    back; no pads. None when the footprint cannot be read
    (swap_clash_details then cannot tell what a test of it answers)."""
    try:
        root = sexpdata.loads(Path(mod_path).read_text(encoding="utf-8"))
        _, front, front_bad = placelib._courtyard_read(root, "F.CrtYd")
        _, back, back_bad = placelib._courtyard_read(root, "B.CrtYd")
        fp = placelib.Footprint(
            ref=ref, fpid=fpid, pos=(float(place["x"]), float(place["y"])),
            angle=float(place.get("deg") or 0.0), side="front",
            attrs=frozenset(), locked=False, pads=[], courtyard_local=None,
            courtyard_missing=True, courtyard_exact=(front, back),
            courtyard_unreadable=(front_bad, back_bad))
        if place.get("side") == "back":
            fp.mirror()
        return fp
    except Exception:  # noqa: BLE001 - unread reads as "cannot tell"
        return None


def _touching(bg, pad) -> list:
    """Tracks and vias whose copper touches `pad` on a layer they share."""
    return [t for t in bg.tracks_of()
            if t.layer in pad.layers and t.poly.intersects(pad.poly)] + \
        [v for v in bg.vias_of()
         if any(v.spans(ly) for ly in pad.layers)
         and v.poly.intersects(pad.poly)]


def swap_keeps_copper(bg, ref: str, new_pads: list,
                      nets: dict[str, str]) -> str | None:
    """None when a footprint swap can keep REF's routing: every old pad that
    copper touches has a pad of the same number in the new footprint, on the
    same net, that the same copper still touches (a pin-compatible package:
    an HTSSOP gaining an exposed pad, a 0603 -> 0805 whose pad overlaps).
    Otherwise the reason it can't, and the swap is del + add."""
    for p in bg.pads_of(ref=ref):
        items = _touching(bg, p)
        if not items or not p.number:
            continue
        same = [n for n in new_pads if n.number == p.number]
        if not same:
            return (f"pad {p.number} is routed and the new footprint has "
                    "no such pad")
        if not _nets_equal(p.net, nets.get(p.number, "")):
            return (f"pad {p.number} is routed and moves from net "
                    f"{p.net!r} to {nets.get(p.number)!r}")
        for it in items:
            lays = [it.layer] if isinstance(it, geom.Track) else \
                [ly for ly in bg.copper_layers if it.spans(ly)]
            if not any(set(lays) & set(n.layers)
                       and it.poly.intersects(n.poly) for n in same):
                where = it.layer if isinstance(it, geom.Track) else \
                    f"via at {list(it.at)}"
                return (f"copper on pad {p.number} ({where}) misses the new "
                        f"pad {p.number}")
    return None


def _rule_layer_ok(layer: str | None, ly: str) -> bool:
    """Whether a .kicad_dru rule's (layer ...) clause takes in copper layer
    ly: outer is F.Cu and B.Cu, inner the In*.Cu layers, a copper layer's
    name only itself; no clause, or a name this reader does not know,
    takes in every layer."""
    if layer == "outer":
        return ly in ("F.Cu", "B.Cu")
    if layer == "inner":
        return re.fullmatch(r"In\d+\.Cu", ly) is not None
    if layer is not None and re.fullmatch(r"(F|B|In\d+)\.Cu", layer):
        return layer == ly
    return True


# the reads of a .kicad_dru condition that a swapped part's old pad and its
# same-numbered new pad on the same net share against the same item: both
# are pads of one net, so a test of these answers the same for either
# (swap_clash_details)
_PAIR_READS = frozenset({"type", "net", "netname", "netclass", "indiffpair",
                         "iscoupleddiffpair"})


def _dru_reads(tree) -> set[str]:
    """What a parsed .kicad_dru condition (route_critical.
    parse_dru_condition) reads of its items, lowercased: "footprint" for a
    footprint test, "type" and "net" for the Type and NetName tests the
    reader parses, the A./B. names in any other text, and "?" for a text
    that names none."""
    kind = tree[0]
    if kind in ("and", "or"):
        return set().union(*(_dru_reads(t) for t in tree[1]))
    if kind == "scope":
        return {"footprint"}
    if kind in ("type", "net"):
        return {kind}
    return {w.lower() for w in re.findall(r"\b[AB]\.(\w+)", tree[1])} or \
        {"?"}


def swap_clash_details(bg, ref: str, new_pads: list, pcb: Path,
                       old_pads: list | None = None, model=None,
                       pending=(), new_fps=None, gone=(),
                       kept: list | None = None) -> list:
    """[(item, info)]: the tracks and vias of another net that ref's new
    pads would short or come closer to than the clearance, each once with
    its worst shortfall: info {clearance_mm, gap_mm, pad, rule}, rule
    naming where the clearance came from. rip_ep.py did this by hand for
    an HTSSOP's new exposed pad and its thermal vias.

    The clearance is taken per pad, item and shared layer (#1146): the
    board floor, raised by either net's netclass or .kicad_dru per-net
    clearance (route_cleanup._rules_clearance with scoped=False), then by
    the largest .kicad_dru clearance rule held to a footprint or a net
    name that may hold for the pair on that layer (route_critical.
    dru_rule_may_hold): a memberOfFootprint or courtyard test
    (dru_condition_scoped), or a NetName test, read per pair as KiCad
    reads it (case-insensitive, wildcards, ||) where the per-net reading
    takes each name literally and leaves out a rule held to a courtyard
    (#924).

    A footprint test reads the board as the update leaves it: model (a
    placelib.PlaceModel of pcb, by default loaded from pcb) without the
    refs in gone (parts the update deletes), with ref and the refs in
    pending (parts it adds or swaps) given their footprint from new_fps
    (ref -> placelib.Footprint, _placed_footprint). Either may be given
    as a function that returns it, called only when a rule first needs a
    footprint, as the default model is loaded (#1163). A courtyard
    test reads both sides' courtyards as KiCad builds them
    (Footprint.courtyard_exact). A part whose footprint is not known (no
    new_fps entry, or a reference two footprints carry) or whose courtyard
    cannot be read answers "cannot tell", which counts as may hold.

    With old_pads (ref's pads before the swap) a pair is left alone where
    a same-numbered old pad, on the same net (_nets_equal) and layer,
    already came at least as close to the item, and every clearance rule
    that reads more of an item than its type and net (_dru_reads) answers
    the same, and knowingly, for the old pad on the board before the
    update as for the new pad after it: the old pad needed the same
    clearance, so copper the board held next to it is not this swap's to
    rip. Where the two net names differ (an open pad's unconnected-* name
    against no net) the per-net clearance must match too, and every
    clearance rule must answer the same. Such pairs go into kept (when a
    list is given) as (item, info), worst per item, for items not ripped
    anyway: a violation the board already had, which DRC still flags."""
    import route_cleanup  # noqa: PLC0415 - heavy, lazy
    import route_critical as rc  # noqa: PLC0415 - heavy, lazy

    per_net, floor, *_ = route_cleanup._rules_clearance(
        pcb, sorted(set(bg.nets)), scoped=False)
    try:
        text = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    except OSError:
        text = ""
    rules, compared, every = [], [], []
    for r in rc.parse_dru_rules(text):
        if r["constraint"] != "clearance":
            continue
        tree = rc.parse_dru_condition(r["condition"])
        if r["nets"] or rc.dru_condition_scoped(tree):
            rules.append((r["min_mm"], r["layer"], tree, r["name"]))
        every.append((r["layer"], tree))
        if _dru_reads(tree) - _PAIR_READS:
            compared.append((r["layer"], tree))
    rules.sort(key=lambda r: -r[0])
    pending = set(pending) - {ref}
    held = {"model": model, "fps": new_fps}

    def loaded(key: str, load):
        """held[key], called first if a function, or load() if None."""
        if held[key] is None:
            held[key] = load()
        elif callable(held[key]):
            held[key] = held[key]()
        return held[key]

    def placed() -> dict:
        fps = loaded("fps", dict)
        return {k: fps.get(k) for k in pending | {ref}}

    def world(over):
        """scope(fn, sel, item) for the board before the update (over
        None) or after it: the refs in gone removed, those over() names
        given their footprint (None: not known)."""
        cache: dict = {}
        picks: dict[str, list] = {}
        courts: dict[tuple, object] = {}
        hits: dict[tuple, bool | None] = {}

        def parts() -> dict:
            if "parts" not in cache:
                m = loaded("model", lambda: placelib.PlaceModel(pcb))
                out = dict(m.footprints)
                if over is not None:
                    for name in gone:
                        out.pop(name, None)
                    out.update(over())
                # KiCad's tests read every footprint a reference names;
                # the model keeps one of them
                out.update(dict.fromkeys(m.dup_refs))
                cache["parts"] = out
            return cache["parts"]

        def fpid(name: str, sel: str) -> str | None:
            # a library id is read only for a selector that can name one
            if ":" not in sel:
                return None
            fp = parts().get(name)
            return None if fp is None else fp.fpid

        def named(sel: str) -> list:
            """[(name, m)]: the parts sel may name, m True or None."""
            if sel not in picks:
                picks[sel] = [
                    (name, m) for name in parts()
                    if (m := rc.dru_selector_matches(
                        sel, name, fpid(name, sel))) is not False]
            return picks[sel]

        def court(name: str, fp, side: int):
            if (name, side) not in courts:
                g = fp.courtyard_exact[side]
                courts[name, side] = None if g is None else prep(
                    affinity.translate(affinity.rotate(
                        g, -fp.angle, origin=(0, 0)), *fp.pos))
            return courts[name, side]

        def meets(fn: str, name: str, item):
            """Whether item meets part name's courtyard (fn court: either
            side's; front or back: that side's), or None: cannot tell."""
            key = (fn, name, id(item))
            if key not in hits:
                fp = parts().get(name)
                if fp is None:
                    hits[key] = None
                else:
                    # courtyard_exact is (own side, far side)
                    sides = (0, 1) if fn == "court" else \
                        (0 if fn == fp.side else 1,)
                    hits[key] = rc._or3(
                        None if fp.courtyard_unreadable[s] else
                        False if fp.courtyard_exact[s] is None else
                        court(name, fp, s).intersects(item.poly)
                        for s in sides)
            return hits[key]

        def scope(fn: str, sel: str, item):
            if fn == "member":
                if not isinstance(item, geom.Pad):
                    return False
                return rc.dru_selector_matches(sel, item.ref,
                                               fpid(item.ref, sel))
            if sel in ("A", "B"):
                # KiCad reads the item itself as the footprint: no pad or
                # copper is one
                return False
            out = False
            for name, m in named(sel):
                hit = meets(fn, name, item)
                if hit is False:
                    continue
                if m is True and hit is True:
                    return True
                out = None
            return out

        return scope

    before = world(None)
    after = world(placed)

    def clr(n, it, ly: str) -> tuple[float, str]:
        c, src = floor, "board floor"
        for net in (n.net, it.net):
            v = per_net.get(net or "")
            if v is not None and v > c:
                c, src = v, f"net {net}"
        for mn, layer, tree, name in rules:
            if mn <= c:
                break
            if _rule_layer_ok(layer, ly) and \
                    rc.dru_rule_may_hold(tree, n, it, after):
                return mn, f"rule {name}"
        return c, src

    def same(tree, p, n, it) -> bool:
        """Whether a rule's answer for old pad p is known and the same
        as for new pad n, each against it on its own board."""
        was = rc.dru_rule_holds(tree, p, it, before)
        return was is not None and \
            was == rc.dru_rule_holds(tree, n, it, after)

    def old_held(n, it, ly: str, d: float) -> bool:
        for p in old_pads or []:
            if p.number != n.number or not _nets_equal(p.net, n.net) or \
                    not p.on(ly) or it.poly.distance(p.poly) > d + 1e-6:
                continue
            # an open pad's net name churns (_nets_equal): where the names
            # differ, a rule or netclass that reads the name may not
            # answer the same, so each must
            if (p.net or None) == (n.net or None):
                tests = compared
            elif per_net.get(p.net or "") == per_net.get(n.net or ""):
                tests = every
            else:
                continue
            if all(same(tree, p, n, it) for layer, tree in tests
                   if _rule_layer_ok(layer, ly)):
                return True
        return False

    top = rules[0][0] if rules else 0.0
    items = bg.tracks_of() + bg.vias_of()
    worst: dict[int, tuple] = {}
    spared: dict[int, tuple] = {}
    for n in new_pads:
        for it in items:
            if n.net and it.net == n.net:
                continue
            lays = [ly for ly in n.layers
                    if (it.layer == ly if isinstance(it, geom.Track)
                        else it.spans(ly))]
            if not lays:
                continue
            d = it.poly.distance(n.poly)
            if d >= max(floor, top, per_net.get(n.net or "", floor),
                        per_net.get(it.net or "", floor)) - 1e-6:
                continue
            for ly in lays:
                c, src = clr(n, it, ly)
                if d >= c - 1e-6:
                    continue
                out = spared if old_held(n, it, ly, d) else worst
                if id(it) not in out or c - d > out[id(it)][0]:
                    out[id(it)] = (c - d, it, {
                        "clearance_mm": round(c, 4), "gap_mm": round(d, 4),
                        "pad": n.number, "rule": src})
    if kept is not None:
        kept.extend((it, info) for k, (_, it, info) in spared.items()
                    if k not in worst)
    return [(it, info) for _, it, info in worst.values()]


def swap_clashes(bg, ref: str, new_pads: list, pcb: Path, **kw) -> list:
    """The items of swap_clash_details (same arguments), without the
    detail."""
    return [it for it, _ in swap_clash_details(bg, ref, new_pads, pcb, **kw)]


# ----------------------------------------------------------- orphan analysis

def _item_key_track(t) -> tuple:
    c = [(round(x, 4), round(y, 4)) for x, y in t.shape.coords]
    if tuple(c[::-1]) < tuple(c):
        c = c[::-1]
    return ("t", t.net, t.layer, round(t.width, 4), tuple(c))


def _item_key_via(v) -> tuple:
    return ("v", v.net, (round(v.at[0], 4), round(v.at[1], 4)),
            round(v.diameter, 4), round(v.drill, 4))


def _net_fills(bg, net: str) -> dict[str, list]:
    fills: dict[str, list] = {}
    for z in bg.zones_of(net):
        for layer, polys in z.fills.items():
            fills.setdefault(layer, []).extend(polys)
    return fills


def copper_analysis(bg, net: str, dead_refs: frozenset,
                    removed: frozenset = frozenset()) -> dict:
    """Which copper of `net` no longer serves anything once `dead_refs`'
    pads are gone?  Connectivity-judged (netconn), copper-touch only:

    - a connected component with NO anchor (surviving pad or zone fill) is
      fully orphaned;
    - inside anchored components, chains are pruned from dangling free ends
      (a segment end supported by nothing: no other segment, no via, no pad,
      no fill). No body-overlap mercy: KiCad 10.0.3 machine-verifiably does
      NOT connect a track to a fill (or another track) it merely laps -
      an unsupported end is dangling to DRC even over its own net's pour
      (adversarial-review probe, extends LEARNINGS [connectivity]);
    - a via is kept only while it has copper attachments (surviving tracks,
      fills, or pads - via-in-pad counts) on >= 2 of its spanned layers.
      netconn has no via<->pad edge, so the component graph gets local
      via-in-pad joins here (a subtree bonded only through via-in-pad must
      not read as unanchored);
    - zero-length segments are invisible to netconn (no edge); each is
      judged directly by copper touch: orphaned iff it rests inside a dead
      ref's pad and touches no surviving same-net copper.
    Pruning runs to fixpoint (a removed via can strand a track and vice
    versa). `removed` (item keys) is copper ripped outright - a swap's
    clashes: it is gone from the start, and what it alone held up goes
    with it. Returns {"tracks": [Track], "vias": [Via]}.
    """
    g = netconn.build(bg, net, include_zones=True)
    all_tracks_list = bg.tracks_of(net)
    vias = bg.vias_of(net)
    pads = [p for p in bg.pads_of(net=net) if p.ref not in dead_refs]
    fills = _net_fills(bg, net)

    # local via-in-pad joins (netconn rule gap): via disk overlapping a pad
    # on a shared layer connects them
    vp = 0
    for vi, v in enumerate(vias):
        vnode = ("via", vi)
        for p in bg.pads_of(net=net):
            if not any(v.spans(ly) for ly in p.layers):
                continue
            if p.poly.distance(Point(v.at)) <= v.diameter / 2.0:
                g.add_edge(vnode, ("pad", p.ref, p.number), 0.0,
                           ("join", f"vp{vp}"))
                vp += 1

    alive_seg = {eid for eid, t in g.tracks.items()
                 if _item_key_track(t) not in removed}
    alive_via = {vi for vi, v in enumerate(vias)
                 if _item_key_via(v) not in removed}

    def node_alive(n) -> bool:
        if n[0] == "pad":
            return n[1] not in dead_refs
        if n[0] == "via":
            return n[1] in alive_via
        return True  # pt / zone

    def seg_nodes(eid):
        t = g.tracks[eid]
        c = list(t.shape.coords)
        return (("pt", (round(c[0][0], netconn.SNAP), round(c[0][1], netconn.SNAP))),
                ("pt", (round(c[-1][0], netconn.SNAP), round(c[-1][1], netconn.SNAP))))

    # ---- component pass: zero-anchor components are wholly orphaned -----
    visited: set = set()
    for start in list(g.adj):
        if start in visited or not node_alive(start):
            continue
        comp_nodes, stack = {start}, [start]
        comp_segs: set = set()
        comp_vias: set = set()
        anchored = False
        while stack:
            u = stack.pop()
            if u[0] == "pad" or u[0] == "zone":
                anchored = True
            if u[0] == "via":
                comp_vias.add(u[1])
            for v, _w, eid in g.adj[u]:
                if eid in g.tracks:
                    if eid not in alive_seg:
                        continue  # ripped: no longer joins anything
                    comp_segs.add(eid)
                elif not (node_alive(v) and node_alive(u)):
                    continue
                if node_alive(v) and v not in comp_nodes:
                    comp_nodes.add(v)
                    stack.append(v)
        visited |= comp_nodes
        if not anchored:
            alive_seg -= comp_segs
            alive_via -= comp_vias

    # ---- prune pass: dangling chains + under-attached vias --------------
    def via_attach_layers(vi: int) -> set[str]:
        v = vias[vi]
        reach = v.diameter / 2.0 + netconn.TOL
        layers: set[str] = set()
        for eid in alive_seg:
            t = g.tracks[eid]
            if t.layer in layers or not v.spans(t.layer):
                continue
            for end in (t.shape.coords[0], t.shape.coords[-1]):
                if math.hypot(end[0] - v.at[0], end[1] - v.at[1]) <= reach:
                    layers.add(t.layer)
                    break
        for layer, polys in fills.items():
            if layer not in layers and v.spans(layer) \
                    and any(p.distance(Point(v.at)) <= v.diameter / 2.0
                            for p in polys):
                layers.add(layer)
        for p in pads:
            for layer in p.layers:
                if layer not in layers and v.spans(layer) \
                        and p.poly.distance(Point(v.at)) <= v.diameter / 2.0:
                    layers.add(layer)
        return layers

    changed = True
    while changed:
        changed = False
        for eid in sorted(alive_seg):
            for node in seg_nodes(eid):
                support = 0
                for v, _w, eid2 in g.adj[node]:
                    if eid2 == eid:
                        continue
                    if eid2 in g.tracks:
                        if eid2 in alive_seg:
                            support += 1
                    elif node_alive(v):
                        support += 1
                if support == 0:
                    alive_seg.discard(eid)
                    changed = True
                    break
        for vi in sorted(alive_via):
            if len(via_attach_layers(vi)) < 2:
                alive_via.discard(vi)
                changed = True

    orphan_tracks = [g.tracks[eid]
                     for eid in sorted(set(g.tracks) - alive_seg)]

    # zero-length crumbs: not in g.tracks at all - judge by copper touch
    dead_pads = [p for p in bg.pads_of(net=net) if p.ref in dead_refs]
    alive_polys = {}  # layer -> list of survivor polys (lazy per layer)

    def survivor_polys(layer):
        if layer not in alive_polys:
            polys = [p.poly for p in pads if layer in p.layers]
            polys += list(fills.get(layer, []))
            polys += [vias[vi].poly for vi in alive_via
                      if vias[vi].spans(layer)]
            polys += [g.tracks[eid].poly for eid in alive_seg
                      if g.tracks[eid].layer == layer]
            alive_polys[layer] = polys
        return alive_polys[layer]

    for t in all_tracks_list:
        if t.length > 0:
            continue
        pt = Point(t.shape.coords[0])
        in_dead = any(t.layer in p.layers and p.poly.distance(pt) <= t.width / 2.0
                      for p in dead_pads)
        if not in_dead:
            continue
        if any(t.poly.intersects(sp) for sp in survivor_polys(t.layer)):
            continue
        orphan_tracks.append(t)

    return {
        "tracks": orphan_tracks,
        "vias": [vias[vi] for vi in sorted(set(range(len(vias))) - alive_via)],
    }


def plan_orphans(bg, dead_refs: list[str], removed: list = ()) -> dict:
    """Orphans caused by deleting `dead_refs`, NET-scoped and baseline-
    subtracted: copper the analysis cannot anchor on the PRE-EDIT board is
    preserved and reported, never silently 'fixed'. That baseline set is not
    necessarily defective copper - machine-verified on the pd-trigger route
    fixture: fan-in lobes whose endpoint lands inside another track's BODY
    (a T-junction) pass DRC 0/0, while netconn deliberately does not join
    endpoint-in-body (conflicting evidence in LEARNINGS [connectivity]).
    Strictness is the safe direction: it can only under-remove, and the
    baseline subtraction keeps every pre-existing case untouched.
    `removed` (Tracks/Vias) is ripped outright with whatever it alone
    connected: a copper-keeping swap's clashes."""
    dead = frozenset(dead_refs)
    gone = frozenset(_item_key_track(t) if isinstance(t, geom.Track)
                     else _item_key_via(t) for t in removed)
    affected = sorted({p.net for p in bg.pads_of()
                       if p.ref in dead and p.net}
                      | {t.net for t in removed if t.net})
    tracks, vias, preexisting = [], [], []
    baselines: dict[str, dict] = {}
    for net in affected:
        base = copper_analysis(bg, net, frozenset())
        baselines[net] = {
            "t": {_item_key_track(t) for t in base["tracks"]},
            "v": {_item_key_via(v) for v in base["vias"]},
        }
        for t in base["tracks"]:
            preexisting.append({"net": net, "kind": "track", "layer": t.layer,
                                "uuid": t.uuid})
        for v in base["vias"]:
            preexisting.append({"net": net, "kind": "via",
                                "at": [v.at[0], v.at[1]], "uuid": v.uuid})
        full = copper_analysis(bg, net, dead, gone)
        for t in full["tracks"]:
            if _item_key_track(t) not in baselines[net]["t"] \
                    or _item_key_track(t) in gone:
                tracks.append(t)
        for v in full["vias"]:
            if _item_key_via(v) not in baselines[net]["v"] \
                    or _item_key_via(v) in gone:
                vias.append(v)
    # every ripped item goes, analysed or not (an unnetted one, a crumb)
    listed = {_item_key_track(t) for t in tracks} | \
        {_item_key_via(v) for v in vias}
    tracks += [t for t in removed if isinstance(t, geom.Track)
               and _item_key_track(t) not in listed]
    vias += [v for v in removed if isinstance(v, geom.Via)
             and _item_key_via(v) not in listed]
    missing = [t for t in tracks if not t.uuid] + \
              [v for v in vias if not v.uuid]
    if missing:
        raise CheckError(
            "orphan copper carries no uuid tokens (board predates KiCad "
            "uuids?) - cannot name items for removal")
    return {"tracks": tracks, "vias": vias, "affected_nets": affected,
            "preexisting": preexisting, "baselines": baselines,
            "rip_nets": sorted({t.net for t in removed if t.net})}


# ------------------------------------------------------------ add placement

def find_mod(fpid: str, fp_paths: list[Path]) -> Path | None:
    lib, name = fpid.split(":", 1)
    for root in fp_paths:
        cand = Path(root) / f"{lib}.pretty" / f"{name}.kicad_mod"
        if cand.is_file():
            return cand
    return None


def _mod_extents(mod_path: Path) -> tuple[float, float, float, float]:
    """Local courtyard bbox of a .kicad_mod (any *.CrtYd graphics), falling
    back to the pad-field bbox + 0.25 mm (placelib's effective-courtyard
    convention). Conservative: pad halves use max(size)/2, rotation-safe."""
    tree = sexpdata.loads(mod_path.read_text(encoding="utf-8",
                                             errors="replace"))
    xs: list[float] = []
    ys: list[float] = []
    for node in tree[1:]:
        if not geom._is_node(node):
            continue
        head = geom._head(node)
        if head not in ("fp_line", "fp_rect", "fp_poly", "fp_circle",
                        "fp_arc"):
            continue
        layer = geom._kid(node, "layer")
        lname = (geom._strs(layer) or [""])[0] if layer is not None else ""
        if "CrtYd" not in lname:
            continue
        if head == "fp_poly":
            pts = geom._kid(node, "pts")
            for x, y in (geom._pts(pts) if pts is not None else []):
                xs.append(x)
                ys.append(y)
        elif head == "fp_circle":
            c = geom._nums(geom._kid(node, "center") or [None])
            e = geom._nums(geom._kid(node, "end") or [None])
            if len(c) >= 2 and len(e) >= 2:
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                xs += [c[0] - r, c[0] + r]
                ys += [c[1] - r, c[1] + r]
        else:
            for key in ("start", "mid", "end"):
                p = geom._kid(node, key)
                if p is not None:
                    n = geom._nums(p)
                    if len(n) >= 2:
                        xs.append(n[0])
                        ys.append(n[1])
    if xs and ys:
        return (min(xs), min(ys), max(xs), max(ys))
    fpo = fplib.parse_footprint(mod_path)
    if not fpo.pads:
        return (-0.5, -0.5, 0.5, 0.5)
    m = 0.25
    halves = [(p.at[0], p.at[1], max(p.size) / 2.0) for p in fpo.pads]
    return (min(x - h for x, _, h in halves) - m,
            min(y - h for _, y, h in halves) - m,
            max(x + h for x, _, h in halves) + m,
            max(y + h for _, y, h in halves) + m)


def validate_placements(doc, adds: list[dict]) -> dict:
    if not isinstance(doc, dict):
        raise CheckError("placements file must be a JSON object "
                         "{ref: {x, y, ...} | {region: [...]}}")
    out = {}
    need = {a["ref"] for a in adds}
    for ref, p in doc.items():
        if not isinstance(p, dict):
            raise CheckError(f"placements[{ref}] must be an object")
        if "region" in p:
            r = p["region"]
            if not (isinstance(r, list) and len(r) == 4
                    and all(isinstance(v, (int, float)) for v in r)
                    and r[0] < r[2] and r[1] < r[3]):
                raise CheckError(f"placements[{ref}].region must be "
                                 "[x1, y1, x2, y2] with x1<x2, y1<y2")
            if p.get("side", "front") != "front":
                raise CheckError(f"placements[{ref}]: region search is "
                                 "front-side only; use exact x/y for back")
        elif "x" in p and "y" in p:
            for k in ("x", "y"):
                if not isinstance(p[k], (int, float)):
                    raise CheckError(f"placements[{ref}].{k} must be a number")
            if p.get("side", "front") not in ("front", "back"):
                raise CheckError(f"placements[{ref}].side must be front|back")
        else:
            raise CheckError(f"placements[{ref}] needs x+y or region")
        if "deg" in p and not isinstance(p["deg"], (int, float)):
            raise CheckError(f"placements[{ref}].deg must be a number")
        out[ref] = p
    return out, sorted(set(doc) - need)


def resolve_placement(ref: str, spec: dict, mod_path: Path,
                      pads: dict[str, str], model: placelib.PlaceModel,
                      bg, warnings: list[str],
                      removed_refs: frozenset = frozenset(),
                      extra_obstacles: list | None = None) -> dict:
    """-> {"x", "y", "deg", "side"} - exact specs pass through (courtyard
    check advisory); region specs scan a grid, requiring courtyard-legal +
    inside the outline, preferring pad-copper-clear candidates nearest the
    region center. Footprints being removed in the same update are not
    obstacles (their spot is free by the time the add lands);
    `extra_obstacles` carries the courtyards of adds already resolved in
    THIS update so two region adds cannot stack on the same spot. On
    return, the chosen courtyard is appended to extra_obstacles."""
    deg = float(spec.get("deg", 0.0))
    x0, y0, x1, y1 = _mod_extents(mod_path)
    base = box(x0 - COURT_CLEAR, y0 - COURT_CLEAR,
               x1 + COURT_CLEAR, y1 + COURT_CLEAR)
    court = affinity.rotate(base, -deg, origin=(0, 0))
    obstacles = [f.extents_abs() for r, f in model.footprints.items()
                 if r not in removed_refs] + list(extra_obstacles or [])
    prepared_obs = [prep(o) for o in obstacles]
    outline = bg.outline

    def legal(px: float, py: float) -> bool:
        c = affinity.translate(court, px, py)
        if not outline.contains(c):
            return False
        return not any(po.intersects(c) for po in prepared_obs)

    fpo = fplib.parse_footprint(mod_path)

    def pad_overlap(px: float, py: float) -> float:
        """Candidate pad copper (inflated by PAD_CLEAR) vs existing copper of
        OTHER nets (own-net overlap is merged copper, fine)."""
        area = 0.0
        for p in fpo.copper_pads:
            dx, dy = geom._rot(p.at[0], p.at[1], -deg)
            half = max(p.size) / 2.0 + PAD_CLEAR
            pol = box(px + dx - half, py + dy - half,
                      px + dx + half, py + dy + half)
            own = pads.get(p.number, "")
            layers = ([ln for ln in bg.copper_layers] if p.drill
                      else ["F.Cu"])
            for layer in layers:
                other = bg.layer_copper(layer, exclude=own) if own \
                    else bg.layer_copper(layer)
                if other.intersects(pol):
                    area += pol.intersection(other).area
        return area

    def finish(px: float, py: float, side: str) -> dict:
        if extra_obstacles is not None:
            extra_obstacles.append(affinity.translate(court, px, py))
        return {"x": px, "y": py, "deg": deg, "side": side}

    if "x" in spec:
        px, py = float(spec["x"]), float(spec["y"])
        if not legal(px, py):
            warnings.append(
                f"{ref}: declared position ({px}, {py}) overlaps an existing "
                "courtyard or leaves the outline - applied as declared")
        return finish(px, py, spec.get("side", "front"))

    rx1, ry1, rx2, ry2 = [float(v) for v in spec["region"]]
    cx, cy = (rx1 + rx2) / 2.0, (ry1 + ry2) / 2.0
    cands = []
    ny = int((ry2 - ry1) / SCAN_STEP) + 1
    nx = int((rx2 - rx1) / SCAN_STEP) + 1
    for iy in range(ny):
        for ix in range(nx):
            px = min(rx1 + ix * SCAN_STEP, rx2)
            py = min(ry1 + iy * SCAN_STEP, ry2)
            if legal(px, py):
                cands.append((math.hypot(px - cx, py - cy), px, py))
    if not cands:
        raise CheckError(
            f"{ref}: no courtyard-legal spot in region {spec['region']} "
            f"(step {SCAN_STEP} mm) - enlarge the region or move parts")
    cands.sort()
    best = None
    for d, px, py in cands:
        ov = pad_overlap(px, py)
        if ov <= 1e-9:
            return finish(px, py, "front")
        if best is None or ov < best[0]:
            best = (ov, px, py)
    ov, px, py = best
    warnings.append(
        f"{ref}: every legal candidate in the region overlaps existing "
        f"copper (best {ov:.2f} mm2 at ({px}, {py})) - the fix loop must "
        "clear it")
    return finish(px, py, "front")


def analyse_swaps(plan: dict, resolved: dict, bg, fp_paths: list[Path],
                  pcb: Path, no_keep: bool, model=None,
                  pending=None) -> list:
    """Decide per swap_new_fp entry whether it keeps the part's routing
    (entry["keep_copper"], or a note saying why not) and return the other
    nets' copper the kept swaps' new pads clash with, to rip. model (the
    board's placelib.PlaceModel) and pending (the refs the update adds or
    swaps; by default the plan's) go to swap_clash_details, with the
    plan's deletions and, for each added or swapped part resolved holds a
    spot for, its new footprint there; a pending part without one is one
    a rule's footprint test cannot be answered for."""
    if pending is None:
        pending = {e["ref"] for e in plan["swap_new_fp"] + plan["add"]}
    gone = {e["ref"] for e in plan["del"]}
    new_fps: dict | None = None
    clashes: list = []
    for entry in plan["swap_new_fp"]:
        ref = entry["ref"]
        want = resolved[ref]
        entry["same_spot"] = (
            abs(want["x"] - entry["pos"][0]) <= POS_TOL
            and abs(want["y"] - entry["pos"][1]) <= POS_TOL
            and min(abs(want["deg"] - entry["deg"]) % 360.0,
                    360.0 - abs(want["deg"] - entry["deg"]) % 360.0)
            <= ANG_TOL
            and want["side"] == entry["side"])
        why = "--no-keep-copper" if no_keep else \
            None if entry["same_spot"] else "placed somewhere new"
        new_pads = []
        if why is None:
            new_pads = placed_pads(
                bg, find_mod(entry["fp_new"], fp_paths), ref, want,
                entry["pads"])
            why = swap_keeps_copper(bg, ref, new_pads, entry["pads"])
        entry["keep_copper"] = why is None
        if why is not None:
            plan["notes"].append(f"{ref}: footprint swap rips its routing "
                                 f"(del + add): {why}")
            continue
        if new_fps is None:
            new_fps = {}
            for e in plan["swap_new_fp"] + plan["add"]:
                if e["ref"] in resolved:
                    fpid = e.get("fp") or e["fp_new"]
                    new_fps[e["ref"]] = _placed_footprint(
                        e["ref"], fpid, find_mod(fpid, fp_paths),
                        resolved[e["ref"]])
        kept: list = []
        found = swap_clash_details(
            bg, ref, new_pads, pcb, old_pads=bg.pads_of(ref=ref),
            model=model, pending=set(pending) - {ref}, new_fps=new_fps,
            gone=gone, kept=kept)
        if kept:
            nets = sorted({it.net or "(no net)" for it, _ in kept})
            plan["notes"].append(
                f"{ref}: kept {len(kept)} track(s)/via(s) of other nets "
                f"({', '.join(nets)}) closer to the new pads than the "
                "clearance, as close as the old pad already came; DRC "
                "still flags them")
        entry["clashes"] = [
            {"kind": "via", "net": it.net, "at": list(it.at), **info}
            if isinstance(it, geom.Via) else
            {"kind": "track", "net": it.net, "layer": it.layer,
             "uuid": it.uuid, **info} for it, info in found]
        clashes += [it for it, _ in found]
    return clashes


# ------------------------------------------------------------------- verify

def _copper_inventory(bg) -> Counter:
    inv: Counter = Counter()
    for t in bg.tracks_of():
        inv[_item_key_track(t)] += 1
    for v in bg.vias_of():
        inv[_item_key_via(v)] += 1
    return inv


def verify_apply(staged: Path, before_bg, before_model, plan: dict,
                 resolved: dict, orphans: dict, netmap: dict,
                 worker_result: dict) -> list[str]:
    problems: list[str] = []
    smodel = placelib.PlaceModel(staged)
    sfields = _board_fields(staged)
    sbg = smodel.bg

    for entry in plan["del"] + plan["swap_new_fp"]:
        ref = entry["ref"]
        gone = ref not in smodel.footprints
        if entry in plan["del"] and not gone:
            problems.append(f"del {ref}: footprint still on board")

    for entry in plan["swap_same_fp"]:
        ref = entry["ref"]
        bf = sfields.get(ref)
        if bf is None:
            problems.append(f"swap {ref}: footprint vanished")
            continue
        if entry["value"] and bf["value"] != entry["value"][1]:
            problems.append(f"swap {ref}: value {bf['value']!r} != "
                            f"{entry['value'][1]!r}")
        for name, (_, new) in entry["fields"].items():
            if bf["fields"].get(name, "") != new:
                problems.append(
                    f"swap {ref}: field {name}={bf['fields'].get(name)!r} "
                    f"!= {new!r}")

    for entry in plan["add"] + plan["swap_new_fp"]:
        ref = entry["ref"]
        want = resolved[ref]
        fp = smodel.footprints.get(ref)
        if fp is None:
            problems.append(f"add {ref}: not on saved board")
            continue
        want_fpid = entry.get("fp") or entry["fp_new"]
        if fp.fpid != want_fpid:
            problems.append(f"add {ref}: fpid {fp.fpid!r} != {want_fpid!r}")
        if abs(fp.pos[0] - want["x"]) > POS_TOL \
                or abs(fp.pos[1] - want["y"]) > POS_TOL:
            problems.append(f"add {ref}: position {fp.pos} != "
                            f"({want['x']}, {want['y']})")
        d = abs(fp.angle - want["deg"]) % 360.0
        if min(d, 360.0 - d) > ANG_TOL:
            problems.append(f"add {ref}: angle {fp.angle} != {want['deg']}")
        if fp.side != want["side"]:
            problems.append(f"add {ref}: side {fp.side} != {want['side']}")

    want_dnp = {e["ref"]: e["dnp"][1] for e in plan["swap_same_fp"]
                if e.get("dnp")}
    want_dnp.update({e["ref"]: bool(e.get("dnp"))
                     for e in plan["add"] + plan["swap_new_fp"]})
    if want_dnp:
        import place_edit
        dnp = place_edit._dnp_refs(staged)
        for ref, want in sorted(want_dnp.items()):
            if (ref in dnp) != want:
                problems.append(f"dnp {ref}: board {ref in dnp} != "
                                f"netlist {want}")

    # full pad->net parity: the board must now MATCH the netlist
    for ref, fp in smodel.footprints.items():
        if "board_only" in fp.attrs:
            continue
        for p in fp.pads:
            if not p.number:
                continue
            want = netmap.get(f"{ref}.{p.number}", "")
            if not _nets_equal(p.net, want):
                problems.append(f"parity {ref}.{p.number}: board "
                                f"{p.net!r} != netlist {want!r}")

    # untouched copper is preserved: after == before - orphans
    expect = _copper_inventory(before_bg)
    for t in orphans["tracks"]:
        expect[_item_key_track(t)] -= 1
    for v in orphans["vias"]:
        expect[_item_key_via(v)] -= 1
    expect = +expect
    got = +_copper_inventory(sbg)
    if got != expect:
        extra = got - expect
        missing = expect - got
        for key in list(extra)[:5]:
            problems.append(f"copper appeared: {key}")
        for key in list(missing)[:5]:
            problems.append(f"copper vanished: {key}")

    # orphan uuids truly gone from the file
    text = staged.read_text(encoding="utf-8")
    for item in orphans["tracks"] + orphans["vias"]:
        if item.uuid and item.uuid in text:
            problems.append(f"orphan uuid {item.uuid} still present")

    # worker-reported silk removals truly gone
    if worker_result.get("removed_texts"):
        import place_edit
        gr_texts, _ = place_edit._parse_board_texts(staged)
        for rt in worker_result["removed_texts"]:
            hits = [t for t in gr_texts
                    if t["text"] == rt["text"] and t["layer"] == rt["layer"]
                    and abs(t["x"] - rt["x"]) <= POS_TOL
                    and abs(t["y"] - rt["y"]) <= POS_TOL]
            if hits:
                problems.append(f"silk text {rt['text']!r} still at "
                                f"({rt['x']}, {rt['y']})")

    # no NEW dangling copper (netconn re-analysis, geometry-keyed - uuids
    # may churn across a pcbnew save)
    for net in orphans["affected_nets"]:
        if net not in set(sbg.nets):
            continue  # net fully removed with its part
        after = copper_analysis(sbg, net, frozenset())
        base = orphans["baselines"][net]
        for t in after["tracks"]:
            if _item_key_track(t) not in base["t"]:
                problems.append(f"NEW dangling track on {net} ({t.layer}) "
                                "after surgery")
        for v in after["vias"]:
            if _item_key_via(v) not in base["v"]:
                problems.append(f"NEW dangling via on {net} at {v.at} "
                                "after surgery")
    return problems


# -------------------------------------------------------------------- apply

def _dangling_count(report: dict) -> int:
    return sum(1 for v in report["violations"]
               if "dangling" in (v.get("check") or ""))


def _ripped_danglers(drc_before: dict, drc_after: dict, staged: Path,
                     before_bg, orphans: dict) -> list:
    """Copper DRC newly calls dangling on a net a swap's clash rip cut, as
    the original board's items. netconn joins overlapping copper where
    KiCad joins only an end inside the other copper, so a short stub off a
    junction can survive the orphan analysis and still dangle in DRC
    (fix_dangle.py chased these by hand). Empty when the new dangling is
    anywhere else: that stays a rollback."""
    nets = set(orphans.get("rip_nets") or ())
    if not nets:
        return []
    def spots(rep):
        return {(round(v["pos"][0], 3), round(v["pos"][1], 3))
                for v in rep["violations"]
                if "dangling" in (v.get("check") or "") and v.get("pos")}
    new = spots(drc_after) - spots(drc_before)
    sbg = geom.BoardGeom.from_file(staged)
    gone = {_item_key_track(t) for t in orphans["tracks"]} | \
        {_item_key_via(v) for v in orphans["vias"]}
    keys = set()
    for v in drc_after["violations"]:
        if "dangling" not in (v.get("check") or "") or not v.get("pos") \
                or (round(v["pos"][0], 3), round(v["pos"][1], 3)) not in new \
                or v.get("net") not in nets:
            continue
        for it in v.get("items") or []:
            u = it.get("uuid")
            keys |= {_item_key_track(t) for t in sbg.tracks_of()
                     if t.uuid == u}
            keys |= {_item_key_via(x) for x in sbg.vias_of() if x.uuid == u}
    return [t for t in before_bg.tracks_of() if _item_key_track(t) in keys
            and _item_key_track(t) not in gone] + \
        [x for x in before_bg.vias_of() if _item_key_via(x) in keys
         and _item_key_via(x) not in gone]


def _unconnected_count(report: dict) -> int:
    return sum(1 for v in report["violations"]
               if (v.get("source") or "") == "unconnected")


def apply_update(pcb: Path, plan: dict, resolved: dict, orphans: dict,
                 comps: list[dict], netmap: dict, fp_paths: list[Path],
                 warnings: list[str]) -> dict:
    cli = env.find_kicad_cli()
    bp = env.find_kicad_python(cli) if cli else None
    if bp is None:
        raise CheckError("KiCad bundled python not found (env.py)")

    before_bg = geom.load_board(pcb, refresh=True)
    before_model = placelib.PlaceModel(pcb)
    drc_before = kc.run_drc(cli, pcb)

    field_updates = []
    for entry in plan["swap_same_fp"]:
        upd = {"ref": entry["ref"]}
        if entry["value"]:
            upd["value"] = entry["value"][1]
        if entry["fields"]:
            upd["fields"] = {k: v[1] for k, v in entry["fields"].items()}
        if entry.get("dnp"):
            upd["dnp"] = entry["dnp"][1]
        field_updates.append(upd)
    adds = []
    for entry in plan["add"] + plan["swap_new_fp"]:
        ref = entry["ref"]
        want = resolved[ref]
        adds.append({"ref": ref,
                     "value": entry["value"],
                     "fpid": entry.get("fp") or entry["fp_new"],
                     "x": want["x"], "y": want["y"], "deg": want["deg"],
                     "side": want["side"], "fields": entry["fields"],
                     "pad_nets": entry["pads"],
                     "pad_pins": entry.get("pins") or {},
                     "dnp": bool(entry.get("dnp")),
                     # a swap keeps the old part's schematic link and,
                     # at the same spot, its field texts (swap_fp.py)
                     "inherit": ({"texts": bool(entry.get("same_spot"))}
                                 if "fp_new" in entry else None)})
    pad_nets = [{"ref": e["ref"], "pad_nets": e["pads"]}
                for e in plan["rewire"]]
    remove_refs = [e["ref"] for e in plan["del"] + plan["swap_new_fp"]]
    d_before = _dangling_count(drc_before)
    for attempt in range(DANGLE_ROUNDS + 1):
        remove_uuids = [t.uuid for t in orphans["tracks"]] + \
                       [v.uuid for v in orphans["vias"]]
        stage = Path(tempfile.mkdtemp(prefix=".aiee_update_",
                                      dir=pcb.parent))
        try:
            staged = stage / pcb.name
            shutil.copy2(pcb, staged)
            for side in (".kicad_pro", ".kicad_dru", ".kicad_prl"):
                sib = pcb.with_suffix(side)
                if sib.is_file():
                    shutil.copy2(sib, stage / sib.name)  # DRC rules
            job = {"verb": "apply_update", "board": str(staged),
                   "out": str(staged),
                   "field_updates": field_updates, "adds": adds,
                   "pad_nets": pad_nets,
                   "fp_paths": [str(p) for p in fp_paths],
                   "remove_refs": remove_refs, "remove_uuids": remove_uuids}
            result = routelib.run_worker(bp, job, stage, worker=WORKER)

            problems = verify_apply(staged, before_bg, before_model, plan,
                                    resolved, orphans, netmap, result)
            if problems:
                raise CheckError("post-apply verify failed (rolled back): "
                                 + "; ".join(problems[:10]))

            refilled = False
            if list(before_bg.zones_of()):
                drc_after = kc.run_drc(cli, staged, refill=True,
                                       save_board=True)
                refilled = True
            else:
                drc_after = kc.run_drc(cli, staged)

            d_after = _dangling_count(drc_after)
            if d_after > d_before:
                more = _ripped_danglers(drc_before, drc_after, staged,
                                        before_bg, orphans)
                if more and attempt < DANGLE_ROUNDS:
                    orphans = {**orphans,
                               "tracks": orphans["tracks"] + [
                                   x for x in more
                                   if isinstance(x, geom.Track)],
                               "vias": orphans["vias"] + [
                                   x for x in more
                                   if isinstance(x, geom.Via)]}
                    continue
                raise CheckError(
                    f"orphan surgery left NEW dangling copper (DRC dangling "
                    f"{d_before} -> {d_after}) - rolled back")

            os.replace(staged, pcb)
            break
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    u_before, u_after = _unconnected_count(drc_before), _unconnected_count(drc_after)
    err = lambda r: sum(  # noqa: E731
        1 for v in r["violations"]
        if v.get("severity") == "error" and (v.get("source") or "") != "unconnected")
    new_errors = err(drc_after) - err(drc_before)
    if new_errors > 0:
        warnings.append(f"DRC gained {new_errors} non-unconnected error(s) - "
                        "run the fix loop, then the drc_routed gate")
    if u_after > u_before:
        warnings.append(f"{u_after - u_before} new unconnected item(s) "
                        "(the added/rewired parts' ratsnest) - route via the "
                        "fix loop")
    return {
        "worker": {k: result[k] for k in ("fields_updated", "added",
                                          "pads_renetted",
                                          "removed_refs", "removed_items",
                                          "removed_texts", "absent_uuids")
                   if k in result},
        "refilled": refilled,
        "ripped": {"tracks": len(orphans["tracks"]),
                   "vias": len(orphans["vias"]), "rounds": attempt + 1},
        "drc": {
            "before": drc_before["counts"], "after": drc_after["counts"],
            "dangling_before": d_before, "dangling_after": d_after,
            "unconnected_before": u_before, "unconnected_after": u_after,
            "new_errors": max(new_errors, 0),
        },
    }


# ---------------------------------------------------------------------- CLI

def record_state(state_path: Path, plan: dict, warnings: list[str]) -> dict:
    import state as state_mod
    st = state_mod.State.load(state_path)
    classes = []
    hold = 0
    for bucket, cls in MODE_CLASSES.items():
        refs = [e["ref"] for e in plan[bucket]]
        if not refs:
            continue
        rec = st.apply_edit(cls, refs)
        classes.append({"class": cls, "refs": refs,
                        "human_hold": rec["human_hold"]})
        hold = max(hold, rec["human_hold"])
    if classes:
        st.save()
    else:
        warnings.append("state: no edit classes recorded (empty plan)")
    return {"file": str(state_path), "classes": classes, "human_hold": hold}


def run(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--placements", default=None,
                    help="JSON {ref: {x,y[,deg][,side]} | {region:[x1,y1,"
                         "x2,y2][,deg]}} - required when the diff adds parts")
    ap.add_argument("--lib", action="append", default=[],
                    help="footprint search root (holds <Lib>.pretty dirs); "
                         "repeatable. Board-adjacent lib/ dirs are searched "
                         "by default")
    ap.add_argument("--no-keep-copper", action="store_true",
                    help="a footprint swap always rips the part's routing "
                         "(del + add), even when the new footprint's pads "
                         "cover the old ones")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the classified plan; mutate nothing")
    ap.add_argument("--state", default=None,
                    help="state.json to record the edit classes in (T7 "
                         "invalidation map)")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)

    pcb = Path(args.pcb).resolve()
    if not pcb.is_file():
        raise CheckError(f"board not found: {pcb}")
    netlist = Path(args.netlist)
    if not netlist.is_file():
        raise CheckError(f"netlist not found: {netlist}")

    try:
        comps, netmap = board_init.parse_netlist(netlist)
    except Exception as exc:
        raise CheckError(f"netlist {netlist} does not parse: {exc}") from exc

    bg = geom.load_board(pcb, refresh=True)
    model = placelib.PlaceModel(pcb)
    warnings: list[str] = []
    plan = build_plan(model, _board_fields(pcb), comps, netmap)

    payload = {"script": "board_update", "status": "pass",
               "board": str(pcb), "netlist": str(netlist),
               "dry_run": bool(args.dry_run), "plan": plan,
               "warnings": warnings}

    fp_paths = [Path(p) for p in args.lib]
    for cand in (pcb.parent / "lib", pcb.parent.parent / "lib"):
        if cand.is_dir():
            fp_paths.append(cand)

    changes = sum(len(plan[k]) for k in MODE_CLASSES)
    if args.dry_run:
        # what each footprint swap would do at its old spot (a --placements
        # entry moves it, and a moved part never keeps its routing)
        moved = set(checklib.load_json(args.placements, "placements file")
                    ) if args.placements else set()
        spots = {e["ref"]: {"x": e["pos"][0], "y": e["pos"][1],
                            "deg": e["deg"], "side": e["side"]}
                 for e in plan["swap_new_fp"]
                 if e["ref"] not in moved and find_mod(e["fp_new"], fp_paths)}
        if spots:
            analyse_swaps({**plan, "swap_new_fp": [
                e for e in plan["swap_new_fp"] if e["ref"] in spots]},
                spots, bg, fp_paths, pcb, args.no_keep_copper, model=model,
                pending={e["ref"] for e in plan["swap_new_fp"]
                         + plan["add"]})
        # classification never reads zone fills - a stale-fill board may
        # still be PLANNED against (apply below does demand freshness)
        if plan["unsupported"]:
            payload["status"] = "violations"
        payload["changes"] = changes
        return payload, args.out_report

    if plan["unsupported"]:
        raise CheckError(
            "unsupported changes present - refusing to apply: "
            + "; ".join(f"{u['ref']} ({u['kind']})"
                        for u in plan["unsupported"]))
    if changes == 0:
        payload["note"] = "board already matches the netlist - nothing to do"
        return payload, args.out_report

    bg.assert_fresh()  # orphan analysis + region scan read zone fills
    if args.state:
        # fail BEFORE mutating if the state file is unusable - after a
        # successful apply a state problem only warns (board is updated)
        import state as state_mod
        state_mod.State.load(Path(args.state))

    resolved: dict[str, dict] = {}
    placed_courts: list = []  # courtyards of adds resolved this update
    add_entries = plan["add"] + plan["swap_new_fp"]
    if add_entries:
        pdoc = checklib.load_json(args.placements, "placements file") \
            if args.placements else {}
        placements, unused = validate_placements(pdoc, add_entries)
        if unused:
            warnings.append(f"placements for refs not being added: "
                            f"{', '.join(unused)}")
        for entry in add_entries:
            ref = entry["ref"]
            fpid = entry.get("fp") or entry["fp_new"]
            mod = find_mod(fpid, fp_paths)
            if mod is None:
                raise CheckError(f"{ref}: footprint {fpid} not found under "
                                 f"{[str(p) for p in fp_paths]} (--lib)")
            mod_pads = {p.number for p in fplib.parse_footprint(mod).pads}
            missing = sorted(set(entry["pads"]) - mod_pads)
            if missing:
                raise CheckError(f"{ref}: netlist pins {missing} have no pad "
                                 f"on {fpid}")
            spec = placements.get(ref)
            if spec is None:
                if "pos" in entry:  # new-fp swap defaults to the old spot
                    spec = {"x": entry["pos"][0], "y": entry["pos"][1],
                            "deg": entry["deg"], "side": entry["side"]}
                else:
                    raise CheckError(f"{ref}: add_part needs a placement "
                                     "(--placements)")
            resolved[ref] = resolve_placement(
                ref, spec, mod, entry["pads"], model, bg, warnings,
                removed_refs=frozenset(
                    e["ref"] for e in plan["del"] + plan["swap_new_fp"]),
                extra_obstacles=placed_courts)

    clashes = analyse_swaps(plan, resolved, bg, fp_paths, pcb,
                            args.no_keep_copper, model=model)
    orphans = plan_orphans(
        bg, [e["ref"] for e in plan["del"] + plan["swap_new_fp"]
             if not e.get("keep_copper")], removed=clashes)
    payload["orphans"] = {
        "affected_nets": orphans["affected_nets"],
        "tracks": len(orphans["tracks"]), "vias": len(orphans["vias"]),
        # copper netconn cannot anchor on the PRE-edit board (T-junction
        # lobes, deliberate stubs, real pre-existing danglers) - kept as-is
        "netconn_unanchored_kept": orphans["preexisting"],
    }

    applied = apply_update(pcb, plan, resolved, orphans, comps, netmap,
                           fp_paths, warnings)
    payload["applied"] = applied
    payload["placements"] = resolved

    if args.state:
        # The board is already updated - a state-file problem must not make
        # the whole run report failure; it becomes a loud warning instead.
        try:
            payload["state"] = record_state(Path(args.state), plan, warnings)
        except Exception as exc:  # noqa: BLE001
            payload["state"] = {"error": f"{type(exc).__name__}: {exc}"}
            warnings.append(
                f"state recording FAILED ({exc}) - the board IS updated; "
                f"record the edit classes manually: state.py edit --class "
                + " / ".join(sorted(cls for b, cls in MODE_CLASSES.items()
                                    if plan[b])))

    stale = sorted({g for b, cls in MODE_CLASSES.items() if plan[b]
                    for g in _class_gates(cls)})
    payload["gates_to_rerun"] = stale
    return payload, args.out_report


def _class_gates(cls: str) -> list[str]:
    import statelib
    return list(statelib.load_map()["edit_classes"][cls]["gates"])


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("board_update", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
