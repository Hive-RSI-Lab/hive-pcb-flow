# review - "look at this board"

Two shapes, and the router picks by whether a `source` was given.

**external** (`--arg source=<path to a KiCad project>`): `intake.py` copies the
project into a normal pcb-flow workspace and runs every gate as a BASELINE. The
source is never written to (sha256-verified before/after) - if the owner wants
fixes applied to their own tree, that is a separate, explicit copy-back.

**workspace** (the project is already `~/dev/boards/<name>/`): re-establish the gates
against the current files, render, and hand a fresh-context reviewer the
summary.

## What the deliverable actually is

`reports/intake-digest.md` - the gate table plus `next_actions`, already phrased
as operator steps. Read that, not the board.

The board's findings are the DELIVERABLE, so intake exits 0 with a failing gate.
Exit 1 means intake itself failed (a gate that could not run, an unresolvable
library, a missing schematic, a degraded design document) - that is your problem
to fix, not the board's.

## Reading the baseline honestly

- `baseline.verify_checks` marks checks `skipped` when their input sidecar is
  absent. Skipped is NOT passed. An imported board usually has no
  `constraints.json`, so return-path / current / diff-pair / creepage / thermal
  never ran. Say which checks are in that state before summarizing "verify".
- intake deliberately plants no `constraints.json` (an empty one would turn an
  honest "skipped" into a vacuous "pass"). Authoring one is a design decision -
  make it with the owner, using `reference/constraints_schema.md`.
- ERC/DRC counts on a foreign board are usually dominated by house-style
  differences (silk, courtyards). Cluster before you narrate:
  `cluster_violations.py` groups by (net, kind, region).
- `formats[]` records what was upgraded. A KiCad-9 project becomes KiCad-10 IN
  THE COPY; the owner's file is untouched and still 9. Mention it - reopening
  the workspace copy in KiCad 9 will fail.

## Then what

Every accepted finding becomes a `fix-finding` run against the gate report that
produced it (`reports/gate-<name>.json`). Library findings go to
`make-footprint`. An imported workspace is a NORMAL workspace: `board_update`,
the fix loop and `resume-phase` all work on it unchanged.

## Run close

A review teaches the skill as much as a build does - usually about the checks
themselves, since a foreign board exercises them on geometry no run of ours
produced. Append those to `~/dev/boards/<b>/LEARNINGS.md` and compile:

    learnings.py compile --workspace ~/dev/boards/<b>

They stay `pending` for a later `promote` pass.

## Do not

- Do not re-run intake on an existing workspace to "refresh" it - `--force`
  replaces everything intake wrote there (kicad/ project files, state.json,
  reports/). It refuses when the workspace holds files intake did not write
  (fw/, import/, kicad/gen/, a README) unless `--keep-foreign` is given, which
  leaves those in place. Use the workspace variant.
- Do not read the .kicad_pcb yourself (rule 1). Renders and JSON only.
