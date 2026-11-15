# The score

`bin/pcb-flow score` measures what this checkout of pcb-flow gets wrong on real
boards held fixed: the findings it raises that aren't defects of the board,
and the known defects it no longer raises. The boards stay the same between
measures, and pcb-flow is the one thing that changes, so a better pcb-flow moves the
number. A true finding on a frozen board is the board's, not pcb-flow's, and
costs nothing: pcb-flow can't move it, and charging it would read a check made
sharp enough to find a new real defect as a loss. The person's test
for the module is "build the hifi board, design new tools, work on them, see
how that improves the board" (hive-pcb-flow chat, 2026-10-28 10:18). This page
is the definition. It is written once: a change to it is a change to what
the Hive measures, and needs to be argued for as one.

## The boards

Each board is a pin, `pinned/<board>.json`:

- **lite-main**, **core** and **ultra-front**: the hi-fi receiver's boards, copied from
  hifi-receiver-2's `boards/<board>` at the commit the pin names, into
  `pinned/<board>/`. The copy leaves out what no gate reads: the fab
  package (except `fab/CPL.csv` and `fab/cpl_visual.json`, which dfm checks
  against), renders and logs. Further boards are added the same way once
  they land.
- **hwde-led-test**: this repo's own board, `boards/hwde-led-test`, where it
  is.
- **digits-v1**, **oasis-v0-2** and **minuet-v3**: open-hardware KiCad boards
  built outside pcb-flow that published errata (justinlindh/digits, AGPL-3.0;
  justbuchanan/oasis, Apache-2.0; brown-studios/minuet, MIT). Only
  their KiCad sources and licence are copied, into `pinned/<board>/kicad/`,
  with a fresh `state.json` (every gate owed), by `bin/pcb-flow pin <board> --from
  <clone> --commit <sha> --path <dir> --url <url> --license <spdx> --errata
  <file>`. A board that published the CPL it sent its assembler is pinned
  with `--cpl <file>` too: the pin copies it to `fab/CPL.csv` and keeps the
  EasyEDA models its checks fetch beside it in `fab/easyeda` (kept across a
  re-pin). dfm then checks that CPL's rotations with no `parts.json`: the
  LCSC numbers come off the board's footprints, and a row with none is a part
  the assembler did not place. A CPL with no models kept beside it and no
  `parts.json` is not checked, so a verdict never depends on what one
  machine has cached. The pin's `source` names the project, its licence and its errata
  file, and lists what in the errata is not a defect of the board (a design
  note, a firmware bug) and so is not in the ledger. Every erratum that is a
  defect of the board is a `real` entry carrying the errata's URL at the
  pinned commit in `source`; one pcb-flow misses is charged as missed, and has an
  issue naming the check that would catch it. pcb-flow has no requirements,
  constraints or parts data for these boards, so the checks that need them
  find nothing: that is part of what they measure.

`bin/score.py` holds the sha256 of every pin and its files (`PINS`). A board
whose files don't match it stops the score and fails the test. A pinned board
changes only through `bin/pcb-flow pin <board> --from <clone> --commit <sha>`,
which copies the board and writes the digests into `bin/score.py`. The commit
that does that names the hifi-receiver-2 commit. Because it changes the
score's own file, the score starts a new baseline there instead of reading
the refresh as a gain or a loss. `bin/pcb-flow pin --digests` re-stamps the
digests alone, after a pin's scripts are classified or this repo's board
changes.

## The ledger

Each pinned board has a ledger of its known findings,
`pinned/known/<board>.json`: `{"board": board, "known": [entry, ...]}`. An entry names one
finding as the score sees it, by where it is rather than how it is worded
(`gate`, `check`, `kind`, `net`, `refs`, `pos` within 0.05 mm; the `message` only for
a finding with none of those), and carries a `verdict` and a `reason`:

- **real**: a defect of the pinned board (a router's staircase, a track
  really under its current, a real ERC conflict). The reason says why.
- **false**: pcb-flow's mistake (a check misreading a footprint, a screen
  that charges a decoupling cap the rail's full current). The reason says
  what pcb-flow gets wrong, so the next agent knows what to fix.
- **?**: not judged yet.

An entry is one finding: two alike take two entries. `bin/pcb-flow score
--draft-known` adds every finding the ledger doesn't hold to it as `?`, for
someone to judge. The ledger is part of the pin's digest, so a verdict
changes `bin/score.py` (`bin/pcb-flow pin --digests` re-stamps it) and starts a
new baseline: judging a finding is a change to what is measured, not a gain.
A re-pin keeps the ledger; entries for defects the new board fixed must go
(they would read as missed), and its new findings are drafted and judged.

## What is charged

For every pinned board, a scratch copy goes through `erc`, `drc_routed`,
`verify` and `dfm`, the same `bin/pcb-flow gate` runs `check-all` makes. Every
shim in `bin/pcb-flow` is off (`HWDE_NO_SHIMS`), so a gate gets no help pcb-flow
doesn't ship. The findings are each finding a gate fails on that no waiver
covers (the board's own `reports/verify-waivers.json`, applied by the gate
as always) and each warning no waiver in that file covers. An info note
(severity `info`: a screen with no limit behind it, or a part of what a
check left `not_judged`) is not a warning and is never charged, though the
gate's result carries it among its `warnings`. A finding that says the
check could not run, not that it judged wrongly, is set aside as not
judged: dfm's `cpl_fetch_failed` (EasyEDA did not answer: a rate limit or
the network). It costs nothing, is no catch, is never drafted into the
ledger, and shows on the board's line as `not judged N (dfm: fetch failed
<refs>, free)`. A part EasyEDA has no model for (`cpl_no_model`) is a
finding and charges as before. The kinds are `NOT_JUDGED` in bin/score.py.
One charge is:

1. each finding the ledger holds **false**;
2. each finding the ledger doesn't hold as judged (`?`, or not in it): an
   unjudged finding may be pcb-flow's mistake until someone says otherwise;
3. each **real** entry no finding matches: a known defect pcb-flow has stopped
   seeing (not for a gate that couldn't run, which is charged as a crash);
4. each warning past a gate's cap (`gate.WARNINGS_CAP`, which the score
   lifts to 100000 through `HWDE_GATE_WARNINGS_CAP`), which would travel
   only as a count and couldn't be judged;
5. each board-local script listed in the pin under `stand_in`: a script in
   the board's files that does what an pcb-flow script should (a router, a
   via fixer, a zone refill), with the gap it fills. A script under
   `design` is the board's own source (its schematic generator, placement
   plan, plane plan, build recipe) and costs nothing. A `.py` or `.sh` the
   pin doesn't classify is listed under `unclassified` and charged until it
   is.

A real finding reported costs nothing.

A gate that can't run (exits other than pass or fail, or writes no result)
charges 100 (`CRASH`), so breaking a gate never hides its findings.

Each planted fault a gate or check lets through charges 100 too (next
section).

The score is minus the charges. Higher is better and 0 is a clean sweep:
every finding real, every known defect found, no stand-ins. The output gives
one line per board (the findings per gate, the known defects found, the
missed, false and unjudged ones by check, and the stand-in scripts), a line
per charged finding as the ledger would name it, then `charges: N`, then the
number.

## The planted faults

The charges come from pcb-flow's own checks, so a check made looser finds less
and the score would rise with nothing on the boards better. Before the
boards, `bin/strength.py` runs faults planted where each gate's verdict comes
from, through the same code the gates run, offline, in seconds:

- **gate**: each gate's criteria in `reference/gates.yaml` (`gate.evaluate`)
  must fail each real finding of a kind it fails on (errors, and for erc and
  drc_routed warnings too), put alone among a real report's own. erc's and
  drc_routed's are KiCad's, kept in `tests/golden/gates/` with the runs they
  came from; verify's and dfm's are the findings their checks raise on the
  verify and dfm faults below, as they raise them today. Nothing in a planted
  report says it is planted, so `gate.py` can let one through only by
  skipping that finding on every board too.
- **verify**: each mutant in `tests/golden/` through the check that owns it.
  Every check in the verify suite must own one, and a mutant whose check
  left the suite is a miss, so dropping a check or its fault costs too.
- **dfm**: the bench's known-answer fixture (`tests/fixtures/stages/`) and
  holes planted at JLC's floors in a shipped drill file: a 0.45 mm gap
  between holes (floor 0.5 mm) and a 0.25 mm drill (floor 0.3 mm).

Each fault let through charges 100, as a gate that can't run does: a check
that misses the fault it was built for isn't measuring the boards, and one
loosened check costs more than the findings it could hide. If the faults
can't run, the score stops instead of printing a number. A new check comes
with its own mutant, which also lets the scorecard see it. The faults and the
corpus are in the score's paths (`.hive/project.json`), so a change to them
starts a new baseline rather than reading as a gain. The gate code
(`scripts/gate.py`, `reference/gates.yaml`) and the checks are not, on
purpose: the Hive scores a merge that changes the score's paths against its
parent with the merge's copy of those paths laid over it, so a loosened gate
or check there would cost both sides the same and read flat, a better one
could never read as a gain, and every change to them would wait on the
person. Kept out, a loosened one costs its planted faults against its
parent, and the planted findings being real ones keeps it from skipping
them. ERC and DRC themselves
are KiCad's and have no planted faults here: a rule set to "ignore" in a
board's project, or a looser `rules_gen` floor, still gets through.

## What moves it

- A fix in pcb-flow that clears a false or unjudged finding on a pinned board
  (a check that stops misreading a footprint, a gate that stops flagging a
  correct track) raises it at once. Nothing has to be re-run or written by
  hand.
- A change that stops reporting a known real defect lowers it, one per
  defect, on top of what the planted faults charge.
- A new check, or a sharper one, that finds a real defect on a pinned board
  lowers it until the ledger holds the defect as real. Put the verdicts in
  the same change (`--draft-known`, judge, `pin --digests`): that starts a
  new baseline instead of reading as a loss. A new check's false findings
  stay charged; that is its cost.
- Judging an unjudged finding real starts a new baseline; it isn't a gain.
- A board that drops a stand-in script, because pcb-flow now does that job, is
  re-pinned at the commit that dropped it. The re-pin is a new baseline.
- Removing a row from `docs/workarounds.md` changes nothing: the score
  doesn't read that file. The table and its probes still hold the shims and
  hand edits to the truth in the test.

## What it doesn't see

- Hand edits in a board's files (a decoupling entry written by hand, a
  `refdes` added to `parts.json`) are part of the pinned board and aren't
  charged. `docs/workarounds.md` keeps tracking them, with a probe each.
- The board-building steps (placement, routing) aren't re-run: they need
  minutes to hours per board and give a different board each time. A tool
  that would build a better board shows up when the board is rebuilt with it
  in its own project and re-pinned.
- Classifying a script as `design` or `stand_in` is a judgment the pin
  records. A reviewer reads a change to it as a change to the score.
- A verdict in the ledger is a judgment too. A false finding judged real
  stops costing (a new baseline, never a gain), so a reviewer reads a
  `real` verdict's reason as closely as a waiver's. A real defect no check
  has raised yet costs nothing either: the ledger holds only what some
  check once found.

## Cost

Every board, every measure. On 2026-10-29 the three boards took 1.5 min in
all with the machine at load 120-250. At load 380, lite-main alone took
11 min (drc_routed's zone refill 7.5 min of it). Both are inside the 30
minutes a check gets. If the boards grow past that, measure only the boards
whose pinned files, or the skill scripts their gates call, changed since the
last kept measure. A gate that can't run is tried three times, 30 s apart.
If KiCad itself didn't start (`EnvError`, such as kicad-cli's version probe
timing out under load), the score gives no number rather than charge pcb-flow
for the machine.
