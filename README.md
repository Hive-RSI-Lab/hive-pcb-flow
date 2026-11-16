# hive-pcb-flow

The pcb-flow module: KiCad hardware design for the Hive, in the Hive org like the other modules. It began as ihsan-sa/pcb-flow's Claude Code skill and is its own now ([where it came from](docs/upstream.md)).

An agent designs a PCB from a written brief: research, architecture, parts, schematic, placement, routing, then the checks (ERC, DRC, the verify suite, JLCPCB DFM on the exported Gerbers), ending in a fab package. The person signs off between stages. Ordering and paying are always the person's.

- [`docs/README.md`](docs/README.md): how an agent uses it in the Hive; [`docs/playbook.md`](docs/playbook.md): the phases, gates and fix loop
- `agents/`: the role prompts the playbook spawns; `scripts/`: the checks and tools; `reference/`: their tables, recipes, checklists and knowledge records
- `tests/`: unit tests for the scripts (`test_env_lookups.py`: the lookups for the Hive's shared tools folder), and `tests/workarounds/`, the probes
- `bin/pcb-flow`: runs the scripts in a Hive sandbox with the Hive's KiCad 10 and freerouting (`pcb-flow check` first)
- The first board made with it: [boards/hwde-led-test/](boards/hwde-led-test/)
- [docs/errata-corpus.md](docs/errata-corpus.md): public boards whose rev B fixed a rev A defect, and which of those defects pcb-flow's checks catch on rev A (`bin/pcb-flow errata_corpus fetch|run|table`)
- [docs/miss-taxonomy.md](docs/miss-taxonomy.md): the corpus misses grouped by the check that would see them, ranked by count times how much of a board the check judges
- [docs/research/](docs/research/README.md): current research turned into checks: `bin/pcb-flow papers` scans the topics in `reference/research/watch.yaml`, reads legitimate open copies (the rest go on [the person's list](docs/research/paywalled.md)), and gives each claim pcb-flow's own verdict, with the test that reached it; only reproduced claims become [guidelines](docs/research/guidelines.md)
- [docs/route-quality.md](docs/route-quality.md): a layout judged as good, not only legal: `route_quality` scores each net class against its shortest 45-degree routes, `route_improve` re-routes the worst nets in rounds and stops when a round stops gaining
- `bin/pcb-flow part_repair`: move, rotate or re-pad parts on a routed board (place_edit's `swap_fp` puts a fixed library footprint in, `renumber` gives pads new numbers): it rips the copper the change spoils in a window round each part, makes the change, routes every net back to as few islands as it had, and keeps the result only if no net has more islands, DRC is no worse and no violation is new near the repair; it writes the op files that replay it (rip, place, route) and a REPORT.md

## Test and score

- **Test**, `bin/pcb-flow test`, run on every change: upstream's unit tests
  for the skill's scripts (in `tests/`, from ihsan-sa/pcb-flow, about 2,400 of
  them) and the workaround probes. It leaves out tests marked `smoke` (a
  live KiCad run) or `net` (the internet), and points `HWDE_BOARDS_ROOT` at
  an empty dir, so tests that want a real board skip. Upstream tests that
  fail here for a cause a brief already owns are expected failures, listed
  with that brief in `tests/conftest.py`; one that starts passing shows as
  XPASS, and its line comes out. `tests/test_workarounds.py` runs every
  probe in `tests/workarounds/` and fails when
  [docs/workarounds.md](docs/workarounds.md) doesn't match them: a cause
  that stands with no row, or a row whose cause is fixed. It also leaves
  out the tests listed as `slow` in `tests/conftest.py` (`SLOW`: the live
  toolchain, whole fixture boards through bench or the gates, intake of a
  whole project); `bin/pcb-flow test-slow` runs those, so run it too on a change
  to the checks, bench, intake or `check_env`. `bin/pcb-flow test-em` runs the long
  full-wave solves test-slow leaves out (`EM` in `tests/conftest.py`, 80 min
  against test-slow's 17 on a machine at load 45), so run it on a change to `scripts/lib/em*`
  or `scripts/lib/solvers.py`. A test past its limit (15 min, 45 in `em`) fails
  by name. `bin/pcb-flow test-smoke` runs the
  `smoke` tests, whole flows on a live KiCad; CI runs them on every PR and
  daily (`.github/workflows/smoke.yml`), and a failed daily run opens an issue. The test keeps its scratch
  files on `/dev/shm` when it has room and nothing in it fsyncs
  (`HWDE_NO_FSYNC`), because on a loaded disk the suite's file writes, not
  its CPU, set its time; it prints its time and its slowest 15 tests at
  the end. A few minutes; pytest arguments pass through
  (`bin/pcb-flow test tests/test_fab.py -x`).
- **Boards**, `bin/pcb-flow check-all`, not the per-change test: copies every
  board under `boards/` to a scratch dir and re-runs its gate steps there
  through `bin/pcb-flow`: `erc`, `drc_routed`, `verify`, `fab` (gerbers, drill,
  BOM and CPL from `fab_export` and `bom_cpl`) and `dfm`. A board runs each
  gate its `state.json` records as passed, and `fab` once it has a package
  in `fab/`. Any step that fails fails it, and it runs the probes too. Its
  time grows with every board, so run it on a change to the gates, the
  shims in `bin/pcb-flow` or a board, and before a release. Both need
  `/hive/tools`'s KiCad and the Hive's uv; on a fresh HOME they build the
  venv first. Inside a Hive turn both, and the scripts that run a whole
  board, the router or a sim (`route_auto`, `route_critical`,
  `place_anneal`, `gate`, `verify_all`, `bench`, `render`, `sim_run`,
  `sim_solve`, `fab_export`), run under `hive agent heavy`: they wait (at most 10
  minutes) while the machine is strained and other turns run one. A command
  already inside one (check-all's gates, a test that runs `bin/pcb-flow`), or a
  Hive without that op yet, runs bare (`tests/test_heavy.py`).
- **Score**, `bin/pcb-flow score`, higher is better: what this checkout's
  gates get wrong on boards held fixed. The hi-fi receiver's lite-main, core and ultra-front,
  pinned in `pinned/` at one hifi-receiver-2 commit, and hwde-led-test go
  through erc, drc_routed, verify and dfm with `bin/pcb-flow`'s shims off. Each board's
  ledger (`pinned/known/<board>.json`) judges its findings: one held real
  is the board's defect and costs nothing; one held false, one not judged
  yet, a known real one no gate reports any more, and each board-local
  script that stands in for pcb-flow is a charge, and the score is minus the
  charges. A fix in pcb-flow that clears a false finding on those boards raises
  it with nothing written by hand; a sharper check's new real findings go
  into the ledger in the same change. A pinned board changes only through `bin/pcb-flow pin`. The whole
  definition is [docs/score.md](docs/score.md). About ten minutes or more,
  under `hive agent heavy`. A looser check finds less, so the score also
  runs pcb-flow's planted faults (`bin/strength.py`: the gate criteria, every
  verify check's mutant in `tests/golden/`, dfm at JLC's floors), and each
  one a check lets through charges 100.
- **Frozen**, `bin/pcb-flow frozen test` (or `test-slow`, `check-all`,
  `score`, or `-- CMD`): runs the command on a copy of the checkout taken
  now (HEAD plus uncommitted edits, deletions and untracked files, not
  ignored ones) and removes the copy after. A run that waits in a heavy
  queue, or an edit made while it runs, then can't change what it judges;
  paths into the checkout point into the copy, and it exits with the
  command's status ([bin/frozen.py](bin/frozen.py)). Budget more time:
  the full `test` took 1177 s frozen against 997 s plain (about a fifth
  more), so raise the `heavy timeout` to match.

MIT, like upstream; see NOTICE for what that doesn't cover.
