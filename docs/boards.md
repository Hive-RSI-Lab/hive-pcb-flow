# Boards built with pcb-flow in other projects

pcb-flow's real boards are built in the projects that need them, not in this
repo: the hi-fi receiver's boards are in hifi-receiver-2's `boards/`. The
score ([score.md](score.md)) re-runs pcb-flow's gates on a copy of each one,
pinned in `pinned/<board>/` at one commit of its project, and charges the
board-local scripts its pin, `pinned/<board>.json`, lists as `stand_in`.

**Who refreshes a pin:** the worker who takes a board through pcb-flow's gates
in its own project, once its main holds the board: `bin/pcb-flow pin <board>
--from <the project's clone> --commit <sha>`, classifying each new script,
in a commit that names the project's commit. The board's ledger,
`pinned/known/<board>.json`, stays: drop the entries for defects the new
board fixed, add its new findings with `bin/pcb-flow score --board <board>
--draft-known`, judge them, and `bin/pcb-flow pin --digests`. Workarounds that have a probe
also get a row in [workarounds.md](workarounds.md), or extend an existing
row's `boards` when it's the same cause. `bin/pcb-flow check-all` re-runs only
this repo's boards; a board here is re-checked by its own project's test.

Running `check_ratings` (or any ledger writer) on a pinned board writes
`ledger.json` into `pinned/<board>/`. The pin digest hashes that tree, so
`test_score`, `test_strength` and `test_golden` then fail with a digest
mismatch and `bin/pcb-flow score` refuses the board. In a test, copy the
board's `kicad/`, `parts/` and `architecture/` into `tmp_path` and run the
check there (`test_lite_main_dividers_judge_clean` in
`tests/test_check_ratings.py`); `HWDE_LEDGER_WRITE=0` also stops the writes
(the test suite sets it). If it already happened, `git checkout -- pinned`.

| board | project | pinned at | where |
|---|---|---|---|
| lite-main | hifi-receiver-2 | 56e91aa | `boards/lite-main`: 2 layers, 130 parts (USB-C PD, ESP32, codec, 2 x 43 W class-D); its snags are `HWDE-SNAGS.md` |
| core | hifi-receiver-2 | 56e91aa | `boards/core`: 4 layers, 278 parts (Plus/Ultra core); its board-local scripts are in `kicad/gen/`, listed in its README |
| ultra-front | hifi-receiver-2 | 56e91aa | `boards/ultra-front`: 2 layers, the Ultra's front panel (LCD and FFC sockets, TCA9535, encoder, buttons, IR); manual routes in `route/manual/` |

**core under the newer checks.** `check_straps` and `check_antenna` (issue
#130) fail core's verify with errors: the GPIO0 strap and six TCA9555 pins
(review-2 DIGITAL-1/2), and fill under U7's antenna on all four layers
(DIGITAL-19). Those are design findings for hifi-receiver-2 to fix, not
workarounds. The score charges them on the pinned core until that project
fixes them and core is re-pinned at the fixing commit.

## Workarounds with no probe

Workarounds these boards needed that have no row in
[workarounds.md](workarounds.md), because there's no probe that could tell
when their cause is fixed. The score charges the board-local scripts among
them as `stand_in` scripts of their pins:

- **core: routing finished by hand.** route_auto left about 40 connections
  open; they were finished with a board-local maze router and rip-up,
  GND-island and via-tap scripts (`kicad/gen/miniroute.py` and the rest).
  A probe needs a dense routing benchmark.
- **core: placement by hand.** place_anneal placed nothing in 22 min on 278
  parts; hand anchors plus a board-local nearest-free-spot placer
  (`kicad/gen/satellites.py`) passed the place gate. Placement is not one of
  the five steps.
- **core: J1 and silk.** `kicad/gen/fix_j1.py` moves the USB-C in from the
  edge and trims its pads; `kicad/gen/silk_clean.py` clears what silk_place
  left.
- **lite-main: route_edit and the antenna.** route_edit didn't check
  clearance or refill zones (`kicad/gen/refill.py`); it refills with
  `--refill` now, and takes the rip/renet/add-path/rename ECOs and
  place_edit the pad/DNP ones that `kicad/gen/rip*.py`, `add_*.py`,
  `rename_net.py`, `pad_*.py` and `set_dnp.py` did by hand (and
  `delete_parts.py` + `rip_dangling.py` are place_edit's `delete` with
  `rip_dangling`, #854); no pcb-flow script draws
  a copper keep-out (`kicad/gen/keepout.py`, the ESP32 antenna and the NPTH
  pegs); stitch_vias' hole-to-hole misses (`kicad/gen/fix_holes.py`).
- **lite-main: footprints by hand.** The stacked RCA's courtyard and pegs
  (`lib/aiee.pretty`) and the USB4105's trimmed pads.
- **core and lite-main: review-2 ECOs by hand.** hifi-receiver-2's
  9da177ca (the first-spin reviews) added about 30 core and 10 lite-main
  scripts: rip-ups (`rip_*`, `ripnet*`), `add_track`/`add_via`, `move_parts`
  and `find_spots`, pad edits (`pad_*`, `eco_rca`), net edits
  (`renet_items`, `rename_net`), `set_dnp`, silk tidies and `via_array`
  power patches. Most do what route_edit, place_edit, board_update or
  planes_gen should take as one op; each pin names the gap.
- **core: the TPA3220 package swap (hifi-receiver-2 PR #68).** Task
  261029-075354 swapped U15/U16 to the exposed-pad HTSSOP with throwaway
  pcbnew scripts, none committed. Each is an pcb-flow op now: `swap_fp` +
  `rip_ep` are board_update's copper-keeping swap (clash rip included),
  `set_dnp` its netlist DNP sync; `widen`, `move_via`, `fix_dangle`
  (`join_end`) and `set5v` (`copy_from`) are route_edit ops; `silk_rm` is
  place_edit `silk_clear` with a `region`; `hide_ref` is `set_text`;
  `rm_uuid`, `rip_near`, `add_via` and `add_tr` are route_edit's
  `remove`/`rip`/`add_via`/`add_path`; `fix45` is route_cleanup's snap.
- **both: EasyEDA's rate limit.** The CPL model cache in `parts/easyeda` was
  filled by retrying until it was complete (brief 261029-041444 makes dfm
  keep and reuse it).
