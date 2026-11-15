# reroute-net - rip up and re-route (or widen) a net

**No script re-routes one named net end to end.** The op list is the per-net
tool (`route_edit.py`); `route_critical.py --only diff|rf|power` re-lays a whole
critical CLASS from constraints; `route_auto.py` is the whole-board Freerouting
pass. (`route_critical.py --nets` is NOT a routing scope - it feeds the
`--pad-window` probe.) Pick the smallest of those that covers the change.

## route_edit - surgery

    {"version":1,"ops":[
      {"op":"remove","uuid":"<track or via uuid>"},
      {"op":"add_track","start":[x,y],"end":[x,y],"width":W,"layer":"F.Cu","net":"+3V3"},
      {"op":"add_via","at":[x,y],"size":S,"drill":D,"net":"GND"}]}

Validated against the board's real nets and layers, applied on a scratch copy,
re-parsed to confirm every add landed and every removed uuid is gone, then
swapped in atomically. An absent removal uuid is a no-op, so re-applying an op
list is safe. A via you replace keeps no teardrops unless you ask: give the
new one `"teardrops":{"length_ratio":0.5, ...}` (teardroplib.ITEM_PARAMS
names, a key left out takes its default; `{}` is all defaults) and the next
refill builds the drops. On a via already there the keys you give are merged
onto its own settings: the rest keep their values, and `{}` only enables. Find the uuid by matching the segment's `start`/`end` in the board
text - violations carry coordinates, not always uuids.

Widening: take the width from `max(required, the same-net copper abutting both
endpoints)` and widen the whole RUN. One short narrow neighbour just moves the
violation.

## Measure before you widen

`route_critical.py --pad-window --nets <net>` routes nothing: it reports the
widest track that can actually reach each pad of the net, against that net's DRU
width floor. Exit 1 means a floor is geometrically UNMEETABLE at that pad - on a
real board the widest legal VBUS track into a USB-C pad was 1.465 mm against a
1.75 mm requirement. Do not neck the run to fit; that is a placement or a pour
answer.

## The class re-lay

`route_critical.py --only diff|rf|power` re-lays that whole class at impedance /
IPC-2152 widths through KRT, and the result is copper Freerouting protects as
guide wires on any later whole-board pass. `route_auto.py` is that whole-board
pass, and its report is where the Freerouting DSN wedge shows up (symptom:
timeout with zero passes, then a KRT fallback). Completion percentage is
informational, never a score. Both refill zones before export and after import -
imported tracks stale the fills, and stale fills mean phantom clearance errors.

## Planes are not tracks

A power net CARRIED BY A PLANE is not trunk-routed on outer layers: the trunk's
vias fragment the plane locally, which is how a real board starved a connector's
thermal spokes. `route_critical` skips plane-carried nets by design - if a rail
needs more copper and it lives on a plane, the answer is a zone change
(`plane_edit`, hold 2), not a fatter track.

## After

Refill, then `drc_routed` (it refuses stale fills), then `verify` - a re-route
moves return paths, so `check_return_path` and `check_current` are the ones that
actually judge the result. Declare `state.py edit --class reroute_net` (hold 1).

## Do not

- Do not hand-edit the .kicad_pcb. route_edit and route_auto are the only
  writers; anything else loses the atomic rollback and the verification pass.
- Do not re-route a diff pair one half at a time - `check_diffpair` measures
  the trunk between matched pads and will read the intermediate state as skew.
