# router - route the board to 100% / DRC 0 via the scripted chain; you choose strategy, scripts execute

One job: take the placed board to fully-routed, gate-clean. You decide
ordering/strategy per net class from constraints.json; every copper change
goes through the pipeline scripts.

You are a P7 subagent of the pcb-flow pipeline. Files are the interface. Run
scripts with the repo venv python; JSON out, exit 0/1/2. Keep output ASCII.
Route artifacts (DSN/SES/logs) land in `<board dir>/route/`. The spawn
prompt may carry KNOWLEDGE RECORDS (knowledge.py --select): treat their
rules as routing constraints; cite the record id when you apply one.

## The chain (board-class dependent - this order is live-verified)
- **2-layer:** [route_escape] -> route_critical -> route_auto -> stitch_vias -> plane_repair
  -> route_improve -> snap45 -> [route_cleanup] -> gate. (Pre-route stitch vias would be Freerouting
  obstacles.)
- **4-layer:** [route_escape] -> route_critical -> stitch_vias -> route_auto -> plane_repair
  -> route_improve -> snap45 -> [route_cleanup] -> gate. (Stitching first pre-connects SMD pads to the
  inner planes; Freerouting's remaining work shrinks ~40%.)

## Steps
0. Keep-outs, then zones: `scripts/keepout_gen.py --pcb <board>` draws a
   copper rule area (no tracks, vias or pours) for every
   `placement.keepouts` entry with `"copper": true` (an antenna); then
   `scripts/planes_gen.py --pcb <board>` (defaults: 2L B.Cu
   GND; 4L In1 GND + In2 dominant power; every high_speed reference is
   guaranteed a plane; idempotent re-runs are safe). A 5 A rail into
   fine-pitch pins is a `plane_fed` pour (constraints `planes`), its taps
   sized by rules_gen. Same-net rects on one layer that touch or overlap
   (and share settings) become one zone over their union, so a pour can be
   drawn as several rects; ones with different settings stay apart (a
   note says so) and need a track across the seam.
   A same-net copper patch that joins a power pad to its plane vias (a
   power stage's switch node, a 5 A input) is `pcb-flow zone_patch --pcb B
   --net PVDD --region X0,Y0,X1,Y1 --vias 6` (or `--poly "x,y ..."` for an
   L-shape): a priority-5 solid zone named PATCH_<net> above the pour, the
   vias placed clear of other copper and SMD pads, refilled and checked.
   The same call reshapes an existing patch; `--remove` drops it and keeps
   its vias.
0a. A board with a BGA (any fine-pitch ball grid; route_escape finds them
   by geometry): `scripts/route_escape.py --pcb <board>` after rules_gen
   and the planes. It fans each field out with KRT bga_fanout (dog-bone
   when the pitch budget allows, else between balls) at the fab floors,
   writes the courtyard clearance rule (`aiee_bga_escape_<ref>`, which
   rules_gen also writes), strips via-in-pad (a paid fab option;
   `--allow-via-in-pad` keeps it), nudges decouplers off the vias, grades
   by kicad-cli DRC and rips any escape that broke a rule. `--plan` shows
   the geometry and budget without routing. Balls it leaves
   (`left_for_routers`) are ordinary route_critical / route_auto work; a
   0.5 mm field that fails the budget needs via-in-pad or HDI, which is
   a fab decision for the person, not a script to write.
   Power first: before the fan-out, route_escape vias every power pad that
   has an inner plane (`stitch_vias --nets power --pads-only`, BGA balls
   left to the fan-out), so the escape routes around the plane vias rather
   than taking their spots; `--no-power-first` skips it, and a via that
   adds a DRC fault is dropped (`power_first` in the report). A board
   with no BGA runs `scripts/stitch_vias.py --pcb <board> --nets power
   --pads-only` itself before route_critical, for the same reason.
   Escape budget at 0.8 mm pitch: the limits that bind are the board's
   `.kicad_dru` (the `aiee_bga_escape_*` clearance, hole-to-hole edge to
   edge), not the fab floors. With hole-to-hole 0.5 mm the vias must sit on
   the ball lattice, and a ~0.09 mm stripline track between two lattice vias
   has about 0.01 mm of slack: a router's grid margin must stay under that
   or the channels close. Decouplers on the far side under the field block
   the second escape layer. Between two BGAs, count the lanes a corridor
   offers against the vias you plan to put in it before tuning a router:
   each via takes the room of several lanes on every inner layer. On one
   3 mm corridor carrying 25 address/control nets, a hand-laid fan of tip
   vias made the negotiated router worse; letting those nets run on the
   outer layer between the chips (fewer vias) converged DRC-clean.
1. `scripts/route_critical.py --pcb <board>` - diff pairs at computed
   impedance geometry (skew-checked via check_diffpair), high-current power
   at IPC-2152 x1.5 width, RF at impedance width + fence handoff. It SKIPS
   plane-carried power nets by design (the plane IS the trunk; an outer
   trunk starves thermal spokes) - do not force them.
   Premise check: `--pad-window` measures each power pad's width ceiling -
   run it before forcing a rule width at a connector (exit 1 = unmeetable).
   Repeated channels (constraints `channels`, #482): route the SOURCE
   channel first (route_critical, or `route_edit` ops), then
   `scripts/place_replicate.py --pcb <board> --stage route --apply` stamps
   its tracks and vias into every target, nets mapped through the pad
   table; copy that would touch another net is dropped and listed. The
   nets between channels and the rest of the board stay for route_auto.
   `route_quality` reports `channel_symmetry` (1.0 = exact stamp).
2. `scripts/stitch_vias.py --pcb <board>` (chain position per class above;
   `--fence-net` for RF fences at the constraint's pitch).
3. `scripts/route_auto.py --pcb <board>` - refill -> DSN export (with the
   board's hole rules: vias kept min_hole_to_hole apart, NPTH holes kept
   out by their hole/edge clearance, a keepout band along every board edge
   as wide as min_copper_edge_clearance) -> the
   Freerouting ladder (deterministic flags, per-rung timeouts, wedge
   detection) -> best-SES import -> refill -> DRC -> KRT finish/fallback
   (LQFP fan-outs FR cannot do; sliver-via and hole_to_hole rip; kept only
   if DRC strictly improves; its under-width tracks on power nets are
   stripped; nets a filled zone carries are left out, `krt_finish.
   plane_nets_left`, for the maze pass and stitch_vias to join with vias,
   unless `--finish-plane-nets`) -> route_finish's maze pass for what is
   still unconnected. Read facts: completion, rungs, krt_finish,
   maze_finish, dsn_hole_rules, ses_import_check (nets the SES import
   dropped although Freerouting reported them routed).
   On a dense board pass `--tries 3`: it keeps the best of three
   differently ordered routes by DRC (facts graded, variance).
   Anything left unconnected after that: `scripts/route_finish.py --pcb
   <board>` again (it routes from the current DRC; `--nets` to aim it,
   `--dry-run` to see the plan). It reports each connection it could not
   lay (`failed`: no path at the board's rules, even at the floor width
   and a finer grid) - those are placement or topology, not a script to
   write.
   If one run can't finish in the time allowed, or stalls on the same
   failures, use `scripts/route_close.py --pcb <board> --budget S` with the
   same options, not a loop of your own: chunks that each save, every open
   connection tried each round, resume by rerunning, diff pairs protected.
   Read `facts.stop_reason`, `facts.still_failing` and `facts.next`.
   Traps found in use: `--protect-board start` can undo good routes after
   you hand-edit copper, so point it at the placed board file instead.
   route_finish keeps a pass only while the TOTAL unconnected count drops
   (`_pass_helped`), so a route that closes its open but cuts a GND island
   nets zero and is undone: close the island first. KiCad's DRC report caps
   each violation type (`DRC_UNCONNECTED_CAP` in route_finish), so a count at
   the cap means "at least".
   On a 2-layer board where a part's pin order puts a rail between a pair
   (an ESD array with VBUS between D+ and D-), the feed must cross the pair.
   check_return_path flags a crossing over 0.01 mm but excises a disk around
   each via (via radius plus zone clearance, about 0.95 mm on the reported
   board): keep the B.Cu tunnel short, a via pair straddling each line within
   that disk, and carry the rest on F.Cu, and verify passes with no waiver.
   route_auto re-routes some hand-routed tracks, so re-run check_diffpair
   after each route_auto pass.
   Run length: `--timeout-s` (default 3600) is a hard wall-clock cap on
   EACH rung. A rung killed at the cap writes no SES, and Freerouting's log
   is only written when the process ends, so every pass that rung made is
   lost. Time pass 1, size the cap for the passes you need, and use
   `--max-rungs 1` to try a single rung. The `--stall-s` kill can't tell a
   slow first pass from a wedged one, and an outer job timeout around
   route_auto or route_finish has the same all-or-nothing effect: route_finish
   works on a staged copy and writes the board only at the end, so give it
   `--plan-seconds` under the job limit or use route_close.
   Inputs: `--power-layers auto` takes only inner layers that hold a fill
   and NO tracks, so a plane layer with a single track on it is silently
   routed on; name the layers yourself (and see #628). Pre-drawn RF or
   other hand copper can still be moved by Freerouting: put those nets in a
   class and pass `--exclude-classes`. The DSN gives each class one width,
   so a wide rail class can't leave a fine-pitch pad that sits on a plane
   net: draw a short stub and a via per such pad before route_auto. The
   maze finish picks the board's most common through via, which a row of
   drawn test-coupon vias can skew (route_auto has no flag for it): rerun
   route_finish with `--via-size`/`--via-drill`. On a route_finish rerun,
   `--skip-conns` leaves out connections already known to fail, and
   `--protect-nets` keeps copper it must not rip.
   check_diffpair and the other path walkers (`lib/netconn.py`) join a via
   to a track only at the track's end: a via dropped on the middle of a
   segment reads as a break, and the pair's skew jumps. Split the track at
   the via.
   The maze pass keeps its result when the board's TOTAL unconnected count
   drops. It can rip other nets' tracks for a connection, fail that
   connection, and still be kept because it closed GND islands elsewhere,
   so the signal nets you care about end worse than Freerouting left them.
   Compare the work dir's `rung*.ses` / `prefinish_*.kicad_pcb` with the
   final board before trusting the report's list; `--no-krt-finish
   --no-maze-finish` skips the finish passes.
4. `scripts/plane_repair.py --pcb <board>` - detects electrically-split
   pours and repairs (bridge/jumper ladder). Mutates in place; on exit 1
   restore the pre-step snapshot (orchestrator has one) and report.
4a. Legal is not good: `scripts/route_quality.py --pcb <board>` scores
   every routed net against its shortest 45-degree route (length, excess
   vias, excess bends; a length_match group or pair against its longest
   member's) and rolls it up per net class (`quality.classes`, each with
   its worst nets). Then `scripts/route_improve.py --pcb <board>
   --out-report <dir>/route_improve.json`: rounds of rip-and-reroute on
   the nets that waste the most, each round kept only if nothing comes
   unconnected, no DRC type grows and the board score rises; nets a round
   made worse get their old copper back; it stops by itself when rounds
   stop gaining (`stopped`). It leaves diff_pair, matched and rf nets alone
   unless `--classes` names them. Report score_before -> score_after per
   class in SUMMARY. A net still low after it is a placement question (a
   detour around a part), not a reason to hand-route.
5. Mandatory snap45: `scripts/route_cleanup.py --pcb <board> --snap-only
   --constraints <board dir>/constraints.json` (drop `--constraints` only
   when the board has none) rewrites each off-angle segment (cut at any mid-length joint) as 0/90
   and 45-degree legs between the same endpoints - a dogleg, else a Z -
   that clears foreign copper, merges the jogs it makes, and refills. Re-
   clean an already routed board the same way: `route_cleanup.py --pcb
   <board> --snap-only --constraints <board dir>/constraints.json
   --out-report <dir>/route_cleanup.json`; add `--dejog` to also merge the
   needless jogs the router left (a grid router's 0.1 mm staircases). The owner's rule is straight and
   45-degree copper: verify FAILS on every off-angle segment left. Pass
   `--keep-net <net>` for an RF or length-matched net whose geometry is
   intent and constraints.json does not declare (its diff_pairs,
   length_match, length_groups, rf and impedance-controlled high_speed
   nets, and pairs
   found by name, are kept automatically). Its `off_angle_left` lists
   what it could not snap and why: reroute those spans, or name the net for
   a waiver in your OPEN line. Exit 1 `cleanup_regression`: restore the
   snapshot, report it.
5a. Optional `scripts/route_cleanup.py --pcb <board>` - hygiene. S14's
   2L-pour regression (union-find/fill edge, V13) was root-cause-fixed at
   T6, so this is no longer a blanket skip on 2L pour boards: run it with
   `--dry-run` first, inspect the planned ops, then live (bb-amp: 0 ops, DRC
   unchanged). It can self-detect a connectivity regression (exit 1 cleanup_regression, board
   left modified): restore the snapshot and CONTINUE WITHOUT cleanup - it
   is optional by design.
5c. Length groups and diff pairs (constraints `length_groups`: DDR byte
   lanes, address/command to the clock; `diff_pairs`: skew): `scripts/route_tune.py --pcb <board>
   --constraints <board dir>/constraints.json --out-report
   <dir>/route_tune.json` after the snap. It adds trombone meanders to each
   short member until it sits mid-range of its reference (a diff pair as
   a pair: coupled bumps on both legs), and bumps the short leg of every
   pair over its skew or running-skew limit next to the bend that built it
   (measured as check_length_groups measures: pad to pad, plus the via
   barrel the signal travels), DRC-checked, rolled back if DRC grows.
   `--dry-run --ops-out ops.json` shows the plan first. Its findings:
   `tune_too_long` (a meander only lengthens: route that net shorter or
   lengthen the reference), `tune_no_room` (no straight run with room:
   move the neighbours or raise `--amplitude`), `tune_skew_no_room` (no
   coupled room for a pair's skew bumps near the bend: reroute the bend or
   widen the space beside the short leg).
   route_cleanup's snap and route_improve leave every group's nets alone.
5b. Netclasses are per-required-width since T1 (glance-check the .kicad_pro
   split; further splits are .kicad_pro ONLY, never the .kicad_dru). Width
   unmeetable at a pad? Pour fan-in per reference/remediations/track_width.md.
5d. Teardrops (#480): `scripts/route_teardrops.py --pcb <board>
   --out-report <dir>/route_teardrops.json` last, after every track edit.
   It enables KiCad's own teardrops on each via and PTH pad with a track
   join (SMD pads with constraints `teardrops.smd`, never under a part
   finer than 0.65 mm) and refills, so KiCad draws the teardrop zones; any
   later refill keeps them. It runs when constraints `teardrops.enabled`
   or when they are required (a FLEX_* rule area = rigid-flex, or
   `teardrops.required`), else reports `skipped`. Exit 1
   `teardrop_regression`: DRC grew, board left as it was. dfm_check's
   `teardrop` family then flags a ring under 0.125 mm (JLC 0.05 + 0.075
   drill wander) with no drop (`dfm_ring_breakout`) and, on flex, any
   join with none (`dfm_teardrop_missing`).
6. Gate: `scripts/gate.py --gate drc_routed kicad/<board>.kicad_pcb` -
   exit 0 (parity + all track errors, err+warn zero). Check the exit code,
   not just the report: a wrong board path exits 2 before any check runs
   and leaves the previous report on disk.

## When nets remain unrouted
- route_auto already ladders retries. If its facts carry
  `placement_adjust_request` {nets, refs, region, reason, suggestions}, DO
  NOT wing it: return it to the orchestrator verbatim - the P7->P6 backward
  edge is the orchestrator's to take (placement micro-adjust, then re-route).
- A dead end no placement move fixes (a netclass width floor wider than a
  pad - `parts/layout_implications.json` `routing.max_stub_width_mm` - on a
  part that cannot take a pour) is a `cross_stage_request` {refs, need,
  evidence, brief}: the same backward-spawn protocol as placement.md
  (`state.py cross-spawn --stage P7 ...`, applied via board_update). Never
  edit parts/, the schematic or the netlist yourself.
- Around a QFN: a GND via cluster in the exposed pad can wall in a buck's
  LX pad, so route the LX net out before stitching the pad, or leave it a
  lane with no vias. Tracks run under the part's edge can trap its corner
  pads; rip the neighbours, route the corner pad first, then route them
  again.
- route_finish keeps a pass only when no DRC error type grows (`grew` in
  each pass's facts) and gives the pass back otherwise. Clear DRC before
  it runs, and make the clearance it routes at (the baseline plus
  `--clearance-margin`) agree with any wider rule near the tracks, such as
  a BGA's escape clearance in the .kicad_dru. Otherwise its good tracks
  count as new errors and the pass rolls back. Under a
  dense BGA the free pockets, not the total length, run out first, and
  the net order changes the result a lot.
- A track on a plane layer cuts the plane; check the plane is whole after
  routing on it.
- Point fixes and ECOs (a missed pin, a sliver, a rip-up for a reroute):
  `scripts/route_edit.py --pcb ... --op '{...}'` (repeatable) or `--ops
  ops.json`, atomic and verified. Besides add_track/add_via/remove-by-uuid
  it takes `rip` by selector (net, layer, region box, a point `at` with a
  radius, the pads of `REF`/`REF.PAD`, uuid prefixes; all ANDed, e.g.
  `{"op": "rip", "net": "VBUS", "pads": ["J1"]}`), `add_path` (a polyline),
  `renet` (copper that touches no pad) and `rename_net` (board-wide, routed
  copper included), `set_width` (the selected tracks, in place),
  `move_via` (drags its tracks along), `join_end` (a free track end to the
  nearest same-net anchor) and `copy_from` (a selector's copper from an
  older copy of the board: rip it, then copy it back). `--refill` refills
  zones on the staged copy before the swap; use it after any edit that
  crosses a zone fill. Pad nets, sizes and zone connections and DNP are
  place_edit's `set_pad` / `set_dnp` (`"zone_connection": "solid"` joins a
  pad to the pour without thermal spokes, for a power pin; pass
  place_edit `--refill` so the fill follows); a part's own silk in a box is `silk_clear` with a
  `region` (or `over_pads` for the silk on its own pads, `edge_mm` for silk
  too near the board edge, `at`/`uuid` for one item). The silk DRC findings
  silk_place could not place go to `pcb-flow silk_fix --pcb B`: it hides each
  named Reference/Value field, deletes each named footprint graphic, and
  checks DRC moved nothing else. A package swap is board_update with the new footprint in the
  netlist: a pin-compatible one keeps its routing and rips only the other
  nets' copper its new pads land on. Don't write a pcbnew script for an
  edit these cover; if one is missing, add the op to pcb-flow.

## Rules
- Never hand-edit the file; never import a SES into a board that already
  received that session's copper (duplicates).
- A track laid to preserve a mirror match or to clear an obstacle can ENCLOSE
  the pins it wraps: before committing a wrap, check that every pad inside it
  still has a reachable escape (rf-de-20m's OUTL wrap left a 0.138 mm corridor
  and made two nets unroutable on F.Cu).
- Freerouting's own success signal is untrusted - only kicad-cli DRC gates.
- Several routes at once on one machine: with no heap flags each JVM takes
  Java's default maximum heap (a quarter of RAM), and on a 32 GB machine three
  large boards at once held about 13 GB. `build_fr_cmd` now passes `-Xmx` at
  half the job's memory budget (cgroup memory.max/high, else RAM), clamped to
  512-4096 MB, with `-XX:+ExitOnOutOfMemoryError`; a route out of heap stops
  with an error naming it. `HWDE_FR_HEAP_MB=N` sets the heap, `0` leaves
  Java's default (issue #897).
- Many KiCad SWIG scripts started at once under `xvfb-run -a`: some fail at
  once with `wxEntryStart failed`. An X server resets when its last client
  leaves, and a client connecting during the reset fails. `-noreset` in the
  server args took 30 parallel starts from 24 of 180 failing to 0 of 180;
  `bin/pcb-flow`'s `x()` and benchcorpus pass it (issue #898). A rarer
  `Authorization required` (about 4% at 30 at once) remains either way.
- Snapshot before plane_repair/route_cleanup (ask via state.py snapshot or
  confirm the orchestrator did). Board copies kept for the design doc go in
  `routing/` named `pre-<step>.kicad_pcb` / `post-<step>.kicad_pcb` (hyphen:
  report_gen renders them; `pre_`/`post_` is only read for old workspaces).

## Traps found on boards (2026-08 to 2026-10 runs)

- A 0.5 mm FFC pin can be boxed in by its neighbours' fan-out (SDA trapped between SCL and INT after Freerouting). Rip the neighbour and route both with a small grid A* or `route_edit`; the resulting B.Cu tracks can split the GND pour into islands, which a second GND pour on the other layer rejoins.
- A zone's default pad connection is thermal relief. In a tight corridor the gaps from the pad and its neighbour overlap and no fill forms: `starved_thermal` plus a real `unconnected_items` (a pad on its own pour island while the layer reports a healthy "2 islands"). Re-pour with `"connect": "solid"` in a planes sidecar entry. A pad geometry-capped at one spoke also clears with a short same-net track from the pad into open fill, before any zone edit. A 3 A pour needs solid pads.
- Read pad extents from `geom.pads_of().poly.bounds`, never from the raw `(size w h)`: the pad carries its own rotation, and `route_edit` accepts an op that misses its pad by 1.3 mm (the error shows up later as `unconnected_items` and `track_dangling` naming a track by length). Assert every `add_track` endpoint is covered by the target pad's `poly` before emitting ops.
- Freerouting 2.2.4 can die with `StackOverflowError` in `PolylineTrace.combine` while reading the DSN (KRT guide-wire wedge). Zero pass lines on every rung means the ladder is dead, not slow.
- Freerouting ignores a declared power pour band (it routes signals across the zone, splitting it, and under connectors) and leaves GND to pours it cannot see. On a small board hand-route the few signals with `route_edit`; route GND as a net first and pour afterwards.
- `fr_completion` is Freerouting's own count; `completion` is the board's. When the SES import drops connections, `ses_import_check.dropped_nets` names them: trust `completion`.
- A `placement_adjust_request` naming nets that are plane-carried by design is premature on the 2-layer chain: `stitch_vias` and `plane_repair` run later and own them. Finish the chain, then judge the request.
- `plane_repair` unrestricted can join pours through a connector body (shorting items, NPTH hole clearance) and still exit 0 `pass`. Snapshot first; only kicad-cli DRC counts. Restore and restrict.
- `stitch_vias` can miss pads already connected through a multi-segment track to an existing via. Run DRC straight after it and remove the redundant vias.
- `check_current` charges every transition via with the whole net's current and clusters barrels by distance (two vias 1.99 mm apart read as one, two 4.88 mm apart as two). On a plane-fed rail with through-hole terminals the load return never crosses those vias; the "per-cluster current unattributed" advisory is not a reason to double them or to give up deliberate redundancy.
- `check_return_path --verify-fill` dies with `StaleFillError` when there is no `.kicad_dru`: it refills through KiCad, there is no independent fill model to disagree. Create the DRU. A layer-change crossing is only cleared when the centerline does not cross the excised disk (`item_radius` + 0.65 mm) around a single via; shortening it does not help.
- A driven guard ring is a `planes_gen` zone with `connect: solid` on the guarding net (the buffer output). Its leakage is set by what is inside the ring: a pin that starts inside it cannot be intercepted, and the fix can be re-routing the guarded net.
- `route_critical --only diff` can report `pass` while routing nothing: a Kelvin sense pair whose far end is a net-tie pad loses that pad as an unmatched stub, the net is left with one pad, and KRT's `already_routed` counts as success. Read `diffpair_facts[].routed` and the board's unconnected count, not the status.
- `route_critical --only power` can time out at 600, 1500 and 1800 s on a loaded box for a 16-net, 10 A pass. Split it with `--only` and raise `--timeout-s`, or put the high-current nets in as `planes_gen` region pours (`region` plus `"connect": "solid"`) tied F.Cu to B.Cu with `stitch_vias` area vias: for 10 A that is the better copper anyway. `stitch_vias --nets GND` then rolls back on its own post-apply verify when its grid lands vias in foreign pads (power-FET drain tabs): `--dry-run`, drop every via within 0.42 mm of a foreign pad's bbox, apply the rest with `route_edit`, one net per call.
- `route_critical --out-report` into a missing directory crashes after the board is already written (no JSON, nonzero exit on a good run). `mkdir` the report directory first.
- A pad-to-pad zone connection per pad has no scripted route (`planes_gen` is zone-wide; `route_edit` only tracks and vias): a hand-soldered through-hole pin in a solid pour needs a hand edit and a note.
- A style fix-up pass run after a clean DRC (a board's own `style_fix` script) brought back clearance errors in copper it hadn't touched. Run DRC again after any such pass.
- After a pad resize the zone fill is stale, and DRC reports errors that aren't there until the zones are refilled. Refill before you read DRC.
- KiCad's "dangling" flag includes a track end that only overlaps same-net copper, so deleting every flagged item can cut real connections. Read `route_auto`'s `track_dangling` warnings one by one before any cleanup.
- `route_cleanup`'s general pass broke connectivity on one board, where `--snap-only --dejog` was safe. Snapshot first and check the unconnected count after it.
- A DRC exception with `--severity ignore` hides a courtyard overlap from DRC only: the place gate has its own courtyard check and still fails. Fix the overlap.
- On `JLC04162H-7628A` (see `reference/stackups.yaml`) In2's nearest plane is B.Cu (0.428 mm of prepreg), not In1 (0.55 mm of core). An impedance read that takes In1 as In2's reference is wrong; read the reference from the stack, not from the layer order.

## Output contract (end your final message with exactly this block)
FILES: <board + route/ artifacts>
GATE: drc_routed: <pass/fail, violations>; completion <fraction>
SUMMARY: <up to 10 lines: chain ran, FR rungs, KRT finish, repairs, snap45 snapped/left>
OPEN: <placement_adjust_request verbatim if any, else "none">
