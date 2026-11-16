#!/usr/bin/env python
"""gate.py - evaluate a pipeline gate (SPEC.md sections 3-4).

    gate.py --gate <name> <input.kicad_sch|.kicad_pcb> [--workspace DIR]
            [--out FILE] [--commit MSG]
    gate.py --gate <name> --report <kc-report.json>          (evaluate, don't re-run)
    gate.py --list

A gate (defined in reference/gates.yaml) runs a kc.py report on an artifact and
applies pass criteria: it FAILS when the count of violations whose severity is
in the gate's fail_severities exceeds max_count. On pass, --commit MSG git-adds
and commits the repo (SPEC section 4: "Git commit after every gate pass"); it
never commits on failure and never pushes.

JSON to stdout (or --out): {script, gate, status:pass|fail, counts, criteria,
failing:[violations that triggered], record_result, commit_result?}. Exit
0 = pass, 1 = fail, 2 = error (bad gate name, toolchain, unreadable input, or
a requested record/commit that did not happen).

T6 additions:
 - --commit stages the board WORKSPACE only (boards/<name>/, derived from the
   input path), never `git add -A` at repo root - a parallel session's dirty
   files no longer ride along in gate commits (LEARNINGS 2026-07-27). Dirty
   paths outside the workspace are listed in commit_result.excluded_dirty.
 - drc gates with drc_options.require_fresh_fills (drc_routed) refuse a
   stale-fill board with exit 2 instead of grading ~375 phantom zone
   clearance errors (LEARNINGS 2026-07-29, hand-run KRT board). On a fresh
   board they also list its stale PATCH_<net> zones (#1424): one that fills
   nothing is a warning drc_routed fails on, one that holds no via of its
   net is info; each names the zone_patch --remove that takes it.
 - an erc gate on a schematic reports pin_types: how many pins of its
   multi-pin symbols carry an electrical type, and the symbols with none. With
   every pin passive (lib_pin_types with no datasheet extracts) the result
   carries a warning, as a 0/0 ERC then checked no pin conflicts at all.
 - --waivers FILE (default <input dir>/reports/verify-waivers.json for the
   verify gate): human-approved residual findings; each waiver needs
   non-empty `reason` and a real `approved` (who/when - a placeholder
   such as PENDING, TBD or N/A is refused, exit 2) and matches on
   (check|kind) + net
   (+ refs subset). Waived violations do not count toward failure but are
   echoed in the result as `waived`.

U2 additions (codex C4):
 - --report is VALIDATED, never trusted: schema version, producing-script
   identity vs the gate's tool, successful status, a present `violations`
   list (a missing key is invalid, not empty), recorded input path (must
   exist, and match the positional input when given), input digest
   (statelib normalized-hash norms) against the file on disk, and a
   generation-time staleness bound (--max-report-age-h, default 24).
   Anything malformed/stale/mismatched -> exit 2, never a pass.
 - --commit requires an explicit board scope: the input must resolve to a
   boards/<name>/ workspace (under env.boards_root(), HWDE_BOARDS_ROOT,
   default ~/dev/boards; the commit goes into the git repo that holds the
   workspace - the boards repo, not hwde). The repo-wide `git add -A` fallback is gone;
   pre-staged index entries outside the scope refuse the commit. A
   requested commit that does not occur (other than a clean nothing-to-do
   skip) is an OPERATIONAL error: the process exits 2 even on gate pass.
 - gates may set `strict: true` (release contexts, gates.yaml
   verify_release/dfm_release): the tool runs in strict coverage mode -
   every applicable check must run, a missing input is a coverage failure
   (codex C7), and the gate refuses (exit 2) instead of grading a partial
   report.

U5 additions (codex H9 - durable waivers):
 - waiver matching hardened: a refs-scoped waiver never matches a refs-less
   finding (the empty set was a subset of everything); a waiver may carry
   `pos` [x, y] and then only matches findings within 0.01 mm of it.
 - waiver entries may carry durability bindings - `artifact` (the report's
   input_digest they were approved against), `checker_version` (checklib
   stamp) and `expires` (ISO date). When present they are validated against
   the report; a waiver whose binding no longer holds matches NOTHING and
   is surfaced in the result as `waivers_invalid`.
 - strict gates REQUIRE full durability on every waiver entry - release
   waivers are evidence, not conveniences. #725: an entry that lacks it, or
   whose binding no longer holds, goes in `waivers_invalid` and on stderr,
   matches nothing, and fails the gate (exit 1, recorded like any fail). It
   used to refuse with exit 2 and a traceback, so nothing was graded or
   recorded.
 - the default verify sidecar resolution now also checks the board
   WORKSPACE reports/ dir (releaselib.waivers_for_input; LEARNINGS
   2026-08-08 - a waiver file in the obvious place was silently ignored).

#731: a waiver file of the wrong shape never ends in a traceback. An entry
that isn't an object, or whose `refs` isn't a list of strings ("U1" included),
goes in `waivers_invalid` by index and matches nothing, on every gate; a strict
gate fails on it. A file with no list of waivers is refused (exit 2) with one
message.

#677: a valid waiver that matches no finding in the report (a mistyped or
renamed net, a finding since fixed) used to do nothing and say nothing. It is
now named in the result as `waivers_unmatched` and on stderr; the grade does
not change, except that a strict gate fails on one (exit 1, recorded like any
fail): a release waiver must describe a finding the board has. Findings below
the fail bar count as matches, since bin/score.py applies waivers to them.

2026-10-05: the place gate applies <workspace>/reports/place-waivers.json by
default too (WAIVER_SIDECARS). Before, only the verify tool looked for a
sidecar, so PCB-0018-A's 14 documented place waivers were never read and its
place gate failed every one of them.

U16 additions (the bb-buck defect - running a gate and recording it were two
steps and only the first was enforced, so a board reached P9 with six passing
gate reports on disk and `gates: {}` in state.json):
 - the gate RECORDS ITSELF. When the input sits inside a workspace (a
   directory holding a state.json - found by walking the input's parents, or
   named outright with --workspace), the result goes through
   state.record_gate: input hashes, attempt count, stale-mark clearing, and
   the U5 digest tooth all apply. Pass AND fail are recorded (the fix loop
   wants every attempt). Recording happens BEFORE --commit, so the state
   update rides in the gate commit.
 - a golden/mutant corpus input has no workspace: nothing is recorded, the
   result says so (`record_result.recorded: false` + reason) and the exit
   code is unchanged. --no-record opts out explicitly.
 - a REQUESTED-but-failed record is an operational error (exit 2), exactly
   like a requested-but-failed commit: a caller keying on exit 0 must not
   believe the evidence was preserved when it was not.

#857: --summary prints a short text summary to stdout in place of the JSON
(which still goes to --out when given), so a board needs no drc.sh of its
own. The first line gives the status and the failing count by check. Next
comes the origin positions are measured from: the top-left of the input
board's Edge.Cuts outline, the corner board_init reports as outline_origin.
Then each failing non-silk finding gets one line: check, net, refs, its
position from that origin, and the item messages. Silk findings are counted
but not listed; a pos-less finding prints "pos ?". Waived findings are only
counted. Exit codes are unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kc  # noqa: E402
from lib import checklib  # noqa: E402
from lib import env  # noqa: E402
from lib import releaselib  # noqa: E402
from lib import statelib  # noqa: E402

import yaml  # noqa: E402

DEFAULT_GATES = (Path(__file__).resolve().parent.parent
                 / "reference" / "gates.yaml")

# Which producing script a --report must come from, per gate tool (C4:
# "producing script identity" - a place report must not grade a dfm gate).
EXPECTED_SCRIPT = {"erc": "kc", "drc": "kc", "verify": "verify_all",
                   "place": "place_metrics", "dfm": "dfm_check",
                   "sim": "sim_run"}

MAX_REPORT_AGE_H = 24.0

# The waiver sidecar each gate tool applies when no --waivers is given,
# resolved workspace-first by releaselib.waivers_for_input. invalidation.yaml
# hashes the same file into that gate's inputs (`waivers`, `place_waivers`),
# so freshness binds the file the gate actually applied. Tools absent here
# take waivers only through --waivers.
WAIVER_SIDECARS = {"verify": "verify-waivers.json",
                   "place": "place-waivers.json"}


def validate_report(gate_name: str, gate: dict, report,
                    cli_input: Path | None,
                    max_age_h: float = MAX_REPORT_AGE_H) -> Path:
    """U2 (codex C4): refuse any --report that is not a fresh, successful,
    matching report for THIS gate and input. Returns the validated input
    path (digest anchor + commit scope). Every refusal raises -> exit 2,
    never a pass."""
    def bad(msg: str):
        raise RuntimeError(f"--report refused: {msg}")

    if not isinstance(report, dict):
        bad("not a JSON object")
    if report.get("script") == "gate" and "report_schema" not in report:
        bad("this is a gate.py result, not the tool's report - write the "
            "report with the tool itself (verify_all --strict / dfm_check "
            "--strict for the release reports) and pass that")
    if report.get("report_schema") != checklib.REPORT_SCHEMA:
        bad(f"report_schema {report.get('report_schema')!r} (expected "
            f"{checklib.REPORT_SCHEMA}) - regenerate with the current tools")
    tool = gate.get("tool")
    expected = EXPECTED_SCRIPT.get(tool)
    if report.get("script") != expected:
        bad(f"produced by {report.get('script')!r} but the {gate_name!r} "
            f"gate's tool {tool!r} expects a {expected!r} report")
    if tool in ("erc", "drc") and report.get("tool") != tool:
        bad(f"kc report tool {report.get('tool')!r} != gate tool {tool!r}")
    status = report.get("status")
    if status not in ("pass", "violations"):
        bad(f"status {status!r} is not a completed run")
    if not isinstance(report.get("violations"), list):
        bad("no 'violations' list - a missing key is invalid, not empty")

    rec = report.get("input")
    if not rec:
        bad("no recorded input path")
    rec_p = Path(rec)
    if cli_input is not None \
            and rec_p.resolve() != Path(cli_input).resolve():
        bad(f"recorded input {rec} does not match the given input "
            f"{cli_input}")
    if not rec_p.exists():
        bad(f"recorded input {rec} does not exist")
    digest = report.get("input_digest")
    if not digest:
        bad("no input_digest")
    # Recompute under the same suffix-keyed norm stamp() used; an unchanged
    # file reproduces the digest exactly (incl. the raw fallback branch).
    cur = statelib.hash_artifact(rec_p, statelib.norm_for_path(rec_p))
    if cur != digest:
        bad(f"input_digest mismatch - {rec_p.name} changed since the "
            "report was generated (stale report)")

    from datetime import datetime, timezone
    gen = report.get("generated_at")
    try:
        gen_dt = datetime.fromisoformat(str(gen))
    except (TypeError, ValueError):
        gen_dt = None
    if gen_dt is None:
        bad(f"unparsable generated_at {gen!r}")
    if gen_dt.tzinfo is None:
        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600.0
    if age_h < -5 / 60.0:
        bad(f"generated_at {gen} is in the future")
    if age_h > max_age_h:
        bad(f"report is {age_h:.1f} h old, staleness bound {max_age_h} h - "
            "re-run the tool")
    return rec_p


# most below-the-bar findings a gate result carries inline (see evaluate);
# bin/score.py lifts it so every finding it judges has a name
WARNINGS_CAP = int(os.environ.get("HWDE_GATE_WARNINGS_CAP") or 200)


def load_gates(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    gates = data.get("gates", {})
    if not gates:
        raise RuntimeError(f"no gates defined in {path}")
    return gates


def run_report_for_gate(gate: dict, input_file: Path) -> dict:
    """Produce a normalized report per the gate's tool. erc/drc go through
    kc.py; verify runs the whole P8 suite via verify_all (its merged summary
    carries the same {violations, counts} shape evaluate() needs)."""
    tool = gate.get("tool")
    strict = bool(gate.get("strict"))
    if tool == "verify":
        return run_verify(input_file, strict=strict)
    if tool == "place":
        import place_metrics  # noqa: E402  (sibling script)
        argv = ["--pcb", str(input_file)]
        if strict:
            argv.append("--strict")
        payload, _ = place_metrics.run(argv)
        if payload.get("status") == "error":
            raise RuntimeError("placement legality could not run: "
                               f"{payload.get('error')}")
        return payload  # sidecars default to the board's own directory
    if tool == "dfm":
        return run_dfm(input_file, strict=strict)
    if tool == "sim":
        return run_sim(input_file)
    if tool == "drc":
        opts = gate.get("drc_options") or {}
        if opts.get("require_fresh_fills"):
            # A stale/unfilled zone makes DRC grade ~375 phantom clearance
            # errors (LEARNINGS 2026-07-29): report staleness, don't grade.
            # Preflight runs BEFORE kicad-cli resolution so the refusal is
            # deterministic. Gates never mutate the board: no refill/save.
            from lib import geom  # noqa: E402
            bg = geom.load_board(input_file)
            try:
                bg.assert_fresh()
            except geom.StaleFillError as exc:
                raise RuntimeError(
                    "zone fills are stale - refill via kc.py drc "
                    f"--refill --save-board, then re-gate ({exc})") from exc
        cli = kc.resolve_cli()
        rep = kc.run_drc(cli, input_file,
                         parity=bool(opts.get("parity")),
                         all_track_errors=bool(opts.get("all_track_errors")))
        if opts.get("require_fresh_fills"):
            extra = patch_findings(bg)
            if extra:
                rep["violations"] = list(rep.get("violations", [])) + extra
                rep["counts"] = kc.summarize(rep["violations"])
        return rep
    cli = kc.resolve_cli()
    if tool == "erc":
        return kc.run_erc(cli, input_file)
    raise RuntimeError(
        "gate tool must be 'erc', 'drc', 'verify', 'place', 'dfm' or 'sim', "
        f"got {tool!r}")


def patch_findings(bg) -> list[dict]:
    """The stale PATCH_<net> zones of a fresh board (geom.stale_patches,
    #1424) as findings beside its DRC's. One that fills nothing after the
    refill is a warning (drc_routed fails on it): zone_patch never writes
    one, so it is left over. One that holds no via of its net but fills is
    info only: a rip may have left it (route_edit names that at edit time,
    #1145), but a corridor strip joins pads with no via by design."""
    from lib import checklib  # noqa: E402
    out = []
    for p in bg.stale_patches():
        where = f"{p['name']} {p['uuid'] or '(no uuid)'}"
        fix = (f"zone_patch --pcb B --net {p['net']} --layer {{ly}} "
               f"--region {p['box']} --remove")
        if p["empty"]:
            ly = p["empty"][0]
            out.append(checklib.violation(
                "stale_patch", "warning", p["at"], ly, p["net"], [],
                f"{where} fills nothing on {', '.join(p['empty'])} after a "
                f"refill: left over, remove it ({fix.format(ly=ly)})",
                "gate", zone=p["uuid"], no_via=p["no_via"]))
        elif p["no_via"]:
            ly = p["layers"][0] if p["layers"] else None
            out.append(checklib.violation(
                "patch_no_via", "info", p["at"], ly, p["net"], [],
                f"{where} holds no via of {p['net']}: a via rip may have "
                f"left it (then {fix.format(ly=ly)}); a strip that joins "
                f"pads needs none", "gate", zone=p["uuid"]))
    return out


def dfm_vendor(board: Path) -> tuple[str | None, str]:
    """(vendor id, where it came from) for the dfm stage's minimums: the
    workspace's chosen vendor (state.json "vendor", from `vendor choose`),
    else the fab of the stackups.yaml build the board's (stackup) block is,
    else (None, "default"): JLC's, the pipeline's default. JLC, chosen or
    matched, is None too, so its run is the default one."""
    from lib import ledgerlib
    vid, src = ledgerlib.chosen_vendor(board.parent.parent), "state.json"
    if not vid:
        # top-level names, as check_vias imports geom: one GeomError class
        lib = str(Path(__file__).resolve().parent / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import geom  # noqa: E402
        import viastub  # noqa: E402
        try:
            vid, known = viastub.board_vendor(geom.load_board(board).stackup)
        except (geom.GeomError, OSError, ValueError):
            vid, known = None, False
        vid, src = (vid, "stackup") if known else (None, "default")
    if not vid or vid == "jlcpcb":
        return None, src if vid else "default"
    return vid, src


def run_dfm(board: Path, strict: bool = False) -> dict:
    """Run dfm_check on the board (P9). Gerbers are exported to a scratch dir,
    so gating never litters the design folder; the schematic beside the board
    (pipeline convention) is the CPL-polarity oracle, and parts.json - beside
    the board or in the workspace's parts/ - drives the BOM-completeness and
    CPL-placement legs, the latter on the workspace's fab/CPL.csv and
    fab/cpl_visual.json when they exist. The minimums are the board's fab's
    (dfm_vendor: the chosen vendor, else its stackup's), JLC's by default;
    the payload's vendor_from says which. strict (U2/C7): a
    sub-check that could not run (open outline, no netlist, no parts.json)
    is a coverage failure -> the payload comes back status error and the
    gate refuses instead of grading the partial report."""
    import dfm_check  # noqa: E402  (sibling script)
    kwargs: dict = {"strict": strict}
    vendor, vendor_from = dfm_vendor(board)
    if vendor:
        kwargs["vendor"] = vendor
    sch = board.with_suffix(".kicad_sch")
    if sch.exists():
        kwargs["schematic"] = sch
    for parts in (board.parent / "parts.json",
                  board.parent.parent / "parts" / "parts.json"):
        if parts.exists():
            kwargs["parts"] = parts
            break
    # The shipped CPL and the fab step's image verdict, when the workspace
    # has them (kicad/<board> -> fab/): placement is checked on what ships.
    fab = board.parent.parent / "fab"
    if (fab / "CPL.csv").is_file():
        kwargs["cpl"] = fab / "CPL.csv"
    if (fab / "cpl_visual.json").is_file():
        kwargs["visual"] = fab / "cpl_visual.json"
    payload = dfm_check.run(board, **kwargs)
    if payload.get("status") == "error":
        raise RuntimeError(f"dfm could not run: {payload.get('error')}")
    payload["vendor"] = vendor or "jlcpcb"
    payload["vendor_from"] = vendor_from
    return payload


def run_sim(sims: Path) -> dict:
    """Run sim_run on a sims DIRECTORY (or a single .cir testbench) - the
    only gate whose input is not a board file. Bounds sidecars live beside
    the testbenches (pipeline convention: kicad/sims/)."""
    import sim_run  # noqa: E402  (sibling script)
    return sim_run.run(sims)


def run_verify(board: Path, strict: bool = False) -> dict:
    """Run verify_all on the board, taking constraints.json / decoupling.json
    from the board's own directory (the pipeline places them there). Reports go
    to a scratch dir so gating never litters the design folder. strict (U2/C7):
    verify_all --strict - a required check that never ran (missing input) is a
    coverage failure, and the gate refuses instead of grading the rest."""
    import shutil
    import tempfile
    import verify_all  # noqa: E402  (sibling script)
    reports = Path(tempfile.mkdtemp(prefix="gate_verify_"))
    try:
        argv = ["--pcb", str(board), "--reports-dir", str(reports)]
        if strict:
            argv.append("--strict")
        # a state_snapshots/ copy walks up to its board's constraints.json
        cons = checklib.find_constraints(board)
        if cons is not None:
            argv += ["--constraints", str(cons)]
        f = board.parent / "decoupling.json"
        if f.exists():
            argv += ["--decoupling", str(f)]
        summary, _ = verify_all.run(argv)
    finally:
        shutil.rmtree(reports, ignore_errors=True)
    if summary.get("status") == "error":
        bad = {n: c.get("reason") or c.get("error")
               for n, c in summary.get("checks", {}).items()
               if c.get("status") in ("error", "skipped_error")}
        raise RuntimeError(f"verification could not run (coverage/errors: "
                           f"{bad})")
    return summary


class WaiverFileRefused(RuntimeError):
    """A sidecar with no list of waivers to grade. main() reports the message
    alone: the file is what's wrong, so a traceback says nothing (#731)."""


def load_waivers(path: Path) -> list[dict]:
    """Read a waiver sidecar's entries. A path that can't be read (#748), a
    file that isn't JSON or is nested deeper than any waiver file needs
    (#748), or whose top level isn't an object holding a `waivers` list, is
    refused (WaiverFileRefused). Every entry passes through for evaluate() to
    grade: one releaselib.waiver_shape_problem refuses (no reason or real
    approval, no check/kind, a check, kind or net that isn't a string, a bad
    refs or pos) goes in `waivers_invalid` by index and matches nothing, so
    one bad entry doesn't take the good ones with it (#736)."""
    too_deep = (f"{path}: nested too deeply - a waiver file needs at most "
                f"{releaselib.WAIVER_MAX_DEPTH} levels")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:  # missing, a directory, no permission
        raise WaiverFileRefused(
            f"{path}: can't be read "
            f"({type(exc).__name__}: {exc.strerror or exc})") from exc
    except RecursionError as exc:
        raise WaiverFileRefused(too_deep) from exc
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
        raise WaiverFileRefused(
            f"{path}: not valid JSON ({type(exc).__name__}: {exc})") from exc
    if releaselib.too_deep(data):
        raise WaiverFileRefused(too_deep)
    waivers = data.get("waivers") if isinstance(data, dict) else None
    if not isinstance(waivers, list):
        raise WaiverFileRefused(
            f"{path}: no 'waivers' list - a waiver file is an object holding "
            f"one, got {data!r:.80}")
    return waivers


def _in_scope(w: dict, v: dict) -> bool:
    """A finding is in a well-shaped entry's scope: see waiver_matches."""
    if w.get("check") and v.get("check") != w["check"] \
            and v.get("source") != w["check"]:
        return False
    if w.get("kind") and v.get("kind") != w["kind"]:
        return False
    if w.get("net") != v.get("net"):
        return False
    if w.get("refs"):
        vrefs = set(v.get("refs") or [])
        if not vrefs or not vrefs <= set(w["refs"]):
            return False
    if w.get("pos") is not None:
        wp, vp = w["pos"], v.get("pos")
        if not (isinstance(vp, (list, tuple)) and len(vp) >= 2):
            return False
        try:
            if abs(wp[0] - float(vp[0])) > releaselib.POS_TOL_MM \
                    or abs(wp[1] - float(vp[1])) > releaselib.POS_TOL_MM:
                return False
        except (TypeError, ValueError):
            return False
    return True


def waiver_matches(w: dict, v: dict) -> bool:
    """Exact (check|kind) + net; refs subset when the waiver lists refs -
    but a refs-scoped waiver NEVER matches a refs-less finding (U5/H9: the
    empty set is a subset of everything, so any ref-scoped waiver used to
    swallow every anonymous finding of the same kind - the rf-de-20m
    footgun). A waiver carrying `pos` additionally requires the finding to
    sit within releaselib.POS_TOL_MM of it. An entry of a bad shape
    (releaselib.waiver_shape_problem) matches nothing."""
    return not releaselib.waiver_shape_problem(w) and _in_scope(w, v)


def unmatched_waiver(i: int, w: dict, violations: list[dict]) -> dict | None:
    """An entry for waiver number i of its file if it matches none of the
    report's findings, with the nets its check/kind does fire on (a typo's
    fix is usually among them); None when it matches one."""
    if any(waiver_matches(w, v) for v in violations):
        return None
    what = {k: w.get(k) for k in ("check", "kind", "net", "refs", "pos")}
    label = " ".join(f"{k}={what[k]}" for k in what if what[k] not in (None, []))
    same = {k: w[k] for k in ("check", "kind") if w.get(k)}
    nets = sorted({str(v.get("net")) for v in violations
                   if _in_scope({**same, "net": v.get("net")}, v)})
    hint = (f"that check/kind fires on nets: {', '.join(nets[:8])}"
            + (f" and {len(nets) - 8} more" if len(nets) > 8 else "")
            if nets else "the report has no finding of that check/kind")
    return {"index": i, "waiver": what,
            "problem": f"waiver {i} [{label}] matches no finding; {hint}"}


def evaluate(gate_name: str, gate: dict, report: dict,
             waivers: list[dict] | None = None) -> dict:
    fail_sev = set(gate.get("fail_severities") or ["error"])
    max_count = int(gate.get("max_count", 0))
    violations = report.get("violations", [])
    failing = [v for v in violations if v.get("severity") in fail_sev]
    waived = []
    invalid_waivers = []
    unmatched_waivers = []
    strict = gate.get("strict")
    if waivers:
        usable = []
        for i, w in enumerate(waivers):
            # U5/H9: release contexts accept DURABLE waivers only - every
            # entry must bind artifact hash + checker version + expiry and
            # all must validate against this report.
            probs = releaselib.durable_waiver_problems(w, report, i) \
                if strict else releaselib.waiver_validity(w, report, i)
            if probs:
                # a waiver whose bindings no longer hold does not match
                # anything - surfaced, never silently applied
                named = w if isinstance(w, dict) else {}
                invalid_waivers.append(
                    {"index": i,
                     "waiver": {k: named.get(k)
                                for k in ("check", "kind", "net", "refs")},
                     "problems": probs})
            else:
                usable.append(w)
                dead = unmatched_waiver(i, w, violations)
                if dead:
                    unmatched_waivers.append(dead)
        still = []
        for v in failing:
            if any(waiver_matches(w, v) for w in usable):
                waived.append(v)
            else:
                still.append(v)
        failing = still
    passed = len(failing) <= max_count \
        and not ((unmatched_waivers or invalid_waivers) and strict)
    result = {
        "script": "gate",
        "gate": gate_name,
        "phase": gate.get("phase"),
        "tool": gate.get("tool"),
        "input": report.get("input"),
        # U5: the underlying report's stamped digest travels into the gate
        # result so state.py record-gate can refuse a result that does not
        # describe the CURRENT artifact (recording a stale/foreign result
        # file as a fresh pass is exactly the C1 poison).
        "input_digest": report.get("input_digest"),
        "status": "pass" if passed else "fail",
        "criteria": {"fail_severities": sorted(fail_sev), "max_count": max_count},
        "counts": report.get("counts", {}),
        "failing_count": len(failing),
        "failing": failing,
    }
    # The findings below the fail bar (warnings, info) travel too: a pass is
    # not "nothing to look at", and verify's merged report lives in a scratch
    # dir gone by the time the caller reads this. Capped so a noisy DRC
    # doesn't swamp the result; warnings_count is always the full number.
    below = [v for v in violations if v.get("severity") not in fail_sev]
    result["warnings_count"] = len(below)
    result["warnings"] = below[:WARNINGS_CAP]
    if len(below) > WARNINGS_CAP:
        result["warnings_truncated"] = True
    if waivers is not None:
        result["waived_count"] = len(waived)
        result["waived"] = waived
        if invalid_waivers:
            result["waivers_invalid"] = invalid_waivers
        if unmatched_waivers:
            result["waivers_unmatched"] = unmatched_waivers
    cov = report.get("coverage")
    if cov is not None:
        # Deep-copy so the report object is never mutated. A check whose
        # error-severity findings were ALL waived moves failed -> waived
        # (verify_all only: its coverage names match violation `source`).
        cov = json.loads(json.dumps(cov))
        if waived and report.get("script") == "verify_all":
            remaining = {v.get("source") for v in failing}
            moved = [n for n in cov.get("failed", []) if n not in remaining]
            if moved:
                cov["failed"] = [n for n in cov["failed"] if n in remaining]
                cov["waived"] = sorted(set(cov.get("waived") or []) | set(moved))
        result["coverage"] = cov
    return result


_PIN_TYPE = re.compile(r"\(pin\s+([a-z_]+)\s+[a-z_]+")
_SHEETFILE = re.compile(r'\(property\s+"Sheet ?[Ff]ile"\s+"([^"]+)"')


def pin_type_coverage(sch: Path) -> dict:
    """How many pins of the symbols a schematic embeds carry a real
    electrical type. ERC finds pin conflicts (output to output, an undriven
    input) only between typed pins: with every IC pin `passive`
    (lib_pin_types with no datasheet extracts) a 0/0 ERC checked none.
    Counts symbols with 3+ pins outside power:, over the whole hierarchy."""
    from lib import fpfix  # noqa: E402  (paren scanner)
    seen: set = set()
    syms: dict[str, dict[str, int]] = {}
    todo = [Path(sch)]
    while todo:
        f = todo.pop()
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        text = f.read_text(encoding="utf-8", errors="replace")
        todo += [f.parent / m for m in _SHEETFILE.findall(text)]
        for head, a, b in fpfix.top_level_nodes(text):
            if head != "lib_symbols":
                continue
            block = text[a:b]
            for h, x, y in fpfix.top_level_nodes(block):
                if h != "symbol":
                    continue
                sub = block[x:y]
                m = re.match(r'\(symbol\s+"([^"]+)"', sub)
                name = m.group(1) if m else "?"
                if name.startswith("power:"):
                    continue
                types: dict[str, int] = {}
                for t in _PIN_TYPE.findall(sub):
                    types[t] = types.get(t, 0) + 1
                syms[name] = types
    ics = {n: t for n, t in syms.items() if sum(t.values()) >= 3}
    pins = sum(sum(t.values()) for t in ics.values())
    passive = sum(t.get("passive", 0) for t in ics.values())
    out = {"symbols": len(ics), "pins": pins, "passive": passive,
           "typed": pins - passive,
           # the symbols ERC can't check at all: every pin passive
           "untyped": sorted(n for n, t in ics.items()
                             if t.get("passive", 0) == sum(t.values()))}
    if pins and passive == pins:
        out["warning"] = (
            f"all {pins} pins of the {len(ics)} multi-pin symbols are typed "
            "passive, so ERC checked no pin conflicts (its 0/0 says nothing "
            "about drivers or outputs): run lib_pin_types with the parts' "
            "datasheet extracts (parts/<lcsc>.json), or review the netlist "
            "pin by pin")
    return out


def workspace_dir(input_file: Path | None) -> Path | None:
    """The board workspace an input path sits in, or None: the directory
    directly under a boards dir (env.boards_root(), or any dir named boards)."""
    if input_file is None:
        return None
    p = Path(input_file).resolve()
    for parent in p.parents:
        if env.is_boards_dir(parent.parent):
            return parent
    return None


def commit_repo(input_file: Path | None) -> Path:
    """The git repo a gate commit goes into: the one holding the input's
    workspace (the boards repo, since boards left hwde), else hwde's own."""
    ws = workspace_dir(input_file)
    if ws is not None:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=str(ws), capture_output=True, text=True)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    return env.repo_root()


# The workspace a gate result belongs in (U16). One definition, in statelib,
# because every self-recording writer asks the same question - board_edit
# (U17) records its outline edit through it too.
find_workspace = statelib.find_workspace


def record_gate_result(gate_name: str, gate: dict, result: dict,
                       input_file: Path | None,
                       explicit_ws: str | None = None) -> dict:
    """Record the result in the workspace's state.json (U16).

    `ok` is the operational verdict: True for a real record OR for "there is
    no workspace to record into" (the corpus case); False means a record that
    should have happened did not, and main() exits 2 on that.
    """
    ws = find_workspace(input_file, explicit_ws)
    if ws is None:
        return {"ok": True, "recorded": False,
                "reason": "no state.json above the input - nothing to record "
                          "(corpus/scratch input)"}
    if gate_name not in statelib.load_map()["gate_inputs"]:
        return {"ok": True, "recorded": False,
                "workspace": str(ws).replace("\\", "/"),
                "reason": f"gate {gate_name!r} has no gate_inputs entry in "
                          "invalidation.yaml - a result with no input hashes "
                          "is not evidence, so it is not recorded"}
    try:
        import state as state_mod  # sibling script (scripts/ is on sys.path)
        st = state_mod.State.load(ws / "state.json")
        g = st.record_gate(gate_name, result, gate.get("phase"))
        st.save()
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return {"ok": False, "recorded": False,
                "workspace": str(ws).replace("\\", "/"),
                "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "recorded": True,
            "workspace": str(ws).replace("\\", "/"), "gate": gate_name,
            "status": g["status"], "attempts": g["attempts"],
            "inputs": (g.get("last") or {}).get("inputs")}


def git_commit_on_pass(msg: str, cwd: Path,
                       input_file: Path | None = None) -> dict:
    """Stage the gate's board workspace and commit (only called on gate pass).

    U2 (codex C4): a gate commit REQUIRES an explicit board scope - the
    boards/<name>/ workspace the input resolves into. There is no repo-wide
    fallback any more (T6's -A-on-clean-tree escape hatch is gone; ladder
    row 59 for why sweeping is poisonous). Pre-staged index entries outside
    the scope also refuse: `git commit` commits the whole index, so a
    parallel session's staged work would ride along silently. Dirty
    worktree paths outside the scope stay unstaged and are listed as
    `excluded_dirty`.

    The result's `ok` field is the operational verdict: True only for a
    real commit or a clean nothing-to-commit skip; False means a REQUESTED
    commit did not occur (main() exits 2 on that even when the gate
    passed). Never pushes.
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=str(cwd),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    ws = workspace_dir(input_file)
    if ws is None:
        return {"committed": False, "ok": False,
                "reason": "--commit requires an explicit board scope: the "
                          "gate input must live under boards/<name>/ "
                          "(the boards root, HWDE_BOARDS_ROOT) "
                          "(the repo-wide fallback is gone)"}
    try:
        ws_rel = ws.resolve().relative_to(Path(cwd).resolve()).as_posix()
    except ValueError:
        return {"committed": False, "ok": False,
                "reason": f"workspace {ws} is outside the repo {cwd}"}

    def in_scope(p: str) -> bool:
        return p == ws_rel or p.startswith(ws_rel + "/")

    # -uall: porcelain must list untracked FILES, not collapsed directories,
    # or the workspace-scope test below cannot see inside a new workspace
    status = git("status", "--porcelain", "-uall")
    if status.returncode != 0:
        return {"committed": False, "ok": False,
                "reason": f"git status failed: {status.stderr.strip()}"}
    if not status.stdout.strip():
        return {"committed": False, "ok": True, "reason": "nothing to commit"}
    dirty = [ln[3:].strip().strip('"').replace("\\", "/")
             for ln in status.stdout.splitlines() if ln.strip()]

    staged = git("diff", "--cached", "--name-only")
    if staged.returncode != 0:
        return {"committed": False, "ok": False,
                "reason": f"git diff --cached failed: {staged.stderr.strip()}"}
    pre_outside = [p.strip().strip('"').replace("\\", "/")
                   for p in staged.stdout.splitlines() if p.strip()]
    pre_outside = [p for p in pre_outside if not in_scope(p)]
    if pre_outside:
        return {"committed": False, "ok": False, "scope": ws_rel,
                "reason": "pre-staged paths outside the workspace (a commit "
                          "would sweep them): "
                          + ", ".join(sorted(pre_outside)[:20])}

    inside = [d for d in dirty if in_scope(d)]
    outside = [d for d in dirty if d not in inside]
    extras: dict = {"scope": ws_rel}
    if outside:
        extras["excluded_dirty"] = outside
    if not inside:
        return {"committed": False, "ok": True,
                "reason": "nothing to commit inside the workspace", **extras}
    add = git("add", "--", ws_rel)
    if add.returncode != 0:
        return {"committed": False, "ok": False,
                "reason": f"git add failed: {add.stderr.strip()}", **extras}
    commit = git("commit", "-m", msg)
    if commit.returncode != 0:
        return {"committed": False, "ok": False,
                "reason": f"git commit failed: {commit.stderr.strip()}",
                **extras}
    rev = git("rev-parse", "--short", "HEAD")
    return {"committed": True, "ok": True, "commit": rev.stdout.strip(),
            **extras}


SUMMARY_MSG_CHARS = 60


def summary_origin(input_file: Path | None) -> tuple[list[float] | None, str]:
    """(origin, where it came from) for --summary positions (#857).

    The top-left of the input board's Edge.Cuts outline - the corner
    board_init reports as outline_origin - so a position reads as "from the
    board's corner". No outline (a schematic, a board without Edge.Cuts) ->
    (None, why): positions stay absolute and the summary says so.
    """
    if input_file is None or Path(input_file).suffix != ".kicad_pcb":
        return None, "absolute (no board input)"
    try:
        from board_init import outline_origin  # sibling script
        o = outline_origin(Path(input_file))
    except Exception as exc:  # noqa: BLE001 - said in the summary, not hidden
        return None, f"absolute (outline unreadable: {type(exc).__name__})"
    if o is None:
        return None, "absolute (no Edge.Cuts outline)"
    return o, f"from the Edge.Cuts outline's top-left ({o[0]:g}, {o[1]:g})"


def _is_silk(check) -> bool:
    return str(check or "").startswith("silk")


def summary_lines(result: dict, origin: list[float] | None,
                  origin_note: str) -> list[str]:
    """The --summary text (#857): what pinned lite-main's drc.sh printed.

    Line 1: gate, status, failing count, and the count by check (silk
    included). Line 2: what positions are measured from. Then one line per
    failing non-silk finding: check, net, refs, position, item messages
    (each cut to SUMMARY_MSG_CHARS). A last line counts the silk findings
    left unlisted and the waived ones, when there are any.
    """
    failing = [v for v in result.get("failing") or [] if isinstance(v, dict)]
    by_check: dict[str, int] = {}
    for v in failing:
        k = str(v.get("check"))
        by_check[k] = by_check.get(k, 0) + 1
    counts = ", ".join(f"{k} {n}" for k, n in
                       sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0])))
    head = (f"{result.get('gate')}: {str(result.get('status')).upper()}, "
            f"{result.get('failing_count', len(failing))} failing")
    lines = [head + (f" ({counts})" if counts else "")]
    lines.append(f"positions: {origin_note}")
    silk = 0
    for v in failing:
        if _is_silk(v.get("check")):
            silk += 1
            continue
        p = v.get("pos")
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = float(p[0]), float(p[1])
            if origin is not None:
                x, y = x - origin[0], y - origin[1]
            where = f"({x:.2f}, {y:.2f})"
        else:
            where = "pos ?"
        refs = ",".join(str(r) for r in v.get("refs") or []) or "-"
        msgs = [str(i.get("msg") or "")[:SUMMARY_MSG_CHARS]
                for i in v.get("items") or [] if isinstance(i, dict)]
        if not msgs and v.get("msg"):
            msgs = [str(v["msg"])[:SUMMARY_MSG_CHARS]]
        lines.append(f"  {v.get('check')}  net {v.get('net') or '-'}  "
                     f"refs {refs}  at {where}  | " + " | ".join(msgs))
    tail = []
    if silk:
        tail.append(f"{silk} silk finding{'s' * (silk != 1)} not listed")
    nw = result.get("waived_count") or 0
    if nw:
        tail.append(f"{nw} waived")
    if tail:
        lines.append("  (" + "; ".join(tail) + ")")
    return lines


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", nargs="?", help="input .kicad_sch or .kicad_pcb")
    ap.add_argument("--gate", help="gate name from gates.yaml")
    ap.add_argument("--gates", default=str(DEFAULT_GATES),
                    help="path to gates.yaml")
    ap.add_argument("--report", help="evaluate this report JSON instead of "
                                     "running the tool (validated: schema, "
                                     "producer, status, input path+digest, "
                                     "age - C4)")
    ap.add_argument("--max-report-age-h", type=float, default=MAX_REPORT_AGE_H,
                    help="refuse a --report generated more than this many "
                         "hours ago (default %(default)s)")
    ap.add_argument("--out", help="write gate result JSON here instead of stdout")
    ap.add_argument("--commit", metavar="MSG",
                    help="git commit the workspace on gate pass with this message")
    ap.add_argument("--waivers", help="waiver sidecar JSON (default: the "
                    "workspace reports/verify-waivers.json for verify gates, "
                    "reports/place-waivers.json for the place gate)")
    ap.add_argument("--workspace", help="workspace whose state.json records "
                    "this result (default: the first parent of the input "
                    "holding a state.json; U16)")
    ap.add_argument("--no-record", action="store_true", dest="no_record",
                    help="evaluate only - do not record the result in "
                         "state.json")
    ap.add_argument("--list", action="store_true", help="list gates and exit")
    ap.add_argument("--summary", action="store_true",
                    help="print a short text summary to stdout instead of the "
                         "JSON (still written to --out): status, failing "
                         "count by check, and one line per non-silk finding "
                         "with its position from the board outline's "
                         "top-left (#857)")
    args = ap.parse_args(argv)

    try:
        gates = load_gates(Path(args.gates))
        if args.list:
            listing = {name: {"phase": g.get("phase"), "tool": g.get("tool"),
                              "description": g.get("description")}
                       for name, g in gates.items()}
            print(json.dumps({"script": "gate", "gates": listing}, indent=2))
            return 0
        if not args.gate:
            ap.error("--gate is required (or use --list)")
        if args.gate not in gates:
            raise RuntimeError(
                f"unknown gate {args.gate!r}; known: {', '.join(gates)}")
        gate = gates[args.gate]

        if args.report:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            # C4: never trust a supplied report - the validated recorded
            # input becomes the effective input (waiver default + commit
            # scope), so a --report evaluation is never scope-less.
            eff_input = validate_report(
                args.gate, gate, report,
                Path(args.input) if args.input else None,
                max_age_h=args.max_report_age_h)
        else:
            if not args.input:
                ap.error("an input file is required unless --report is given")
            eff_input = Path(args.input)
            report = run_report_for_gate(gate, eff_input)
            if report.get("status") == "error":
                raise RuntimeError(
                    f"{gate.get('tool')} report status is error: "
                    f"{report.get('error')}")

        waivers = None
        if args.waivers:
            waivers = load_waivers(Path(args.waivers))
        elif gate.get("tool") in WAIVER_SIDECARS and eff_input is not None:
            # U5: shared resolution with attest - the board workspace
            # reports/ dir first, then the input's own reports/ dir (the
            # silently-ignored-waivers footgun, LEARNINGS 2026-08-08)
            sidecar = releaselib.waivers_for_input(
                Path(eff_input), WAIVER_SIDECARS[gate["tool"]])
            if sidecar is not None:
                waivers = load_waivers(sidecar)

        result = evaluate(args.gate, gate, report, waivers=waivers)
        if gate.get("tool") == "erc" and eff_input is not None \
                and Path(eff_input).suffix == ".kicad_sch":
            cov = pin_type_coverage(Path(eff_input))
            result["pin_types"] = cov
            if cov.get("warning"):
                result.setdefault("warnings", []).append(cov["warning"])

        # U16: record BEFORE the commit so state.json rides in it. Pass and
        # fail both record - the fix loop's evidence is every attempt.
        if not args.no_record:
            result["record_result"] = record_gate_result(
                args.gate, gate, result, eff_input, args.workspace)

        if result["status"] == "pass" and args.commit:
            result["commit_result"] = git_commit_on_pass(
                args.commit, commit_repo(eff_input), input_file=eff_input)
    except WaiverFileRefused as exc:
        print(json.dumps({"script": "gate", "status": "error",
                          "error": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"script": "gate", "status": "error",
                          "error": traceback.format_exc()}))
        return 2

    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    if args.summary:
        origin, note = summary_origin(eff_input)
        print("\n".join(summary_lines(result, origin, note)))
    elif not args.out:
        print(text)

    # one-line human summary to stderr; stdout stays pure JSON
    n = result.get("failing_count", 0)
    print(f"gate {args.gate}: {result['status'].upper()} "
          f"({n} failing, {result.get('warnings_count', 0)} below the bar"
          f" / {result['counts'].get('total', 0)} total)",
          file=sys.stderr)
    for w in result.get("warnings", []):
        print(f"gate {args.gate}: warning: {w}", file=sys.stderr)
    for iw in result.get("waivers_invalid", []):
        for prob in iw["problems"]:
            print(f"gate {args.gate}: "
                  f"{'FAIL' if gate.get('strict') else 'warning'}: waiver "
                  f"{iw['index']} ignored (invalid): {prob}", file=sys.stderr)
    for u in result.get("waivers_unmatched", []):
        print(f"gate {args.gate}: "
              f"{'FAIL' if gate.get('strict') else 'warning'}: {u['problem']}",
              file=sys.stderr)
    rr = result.get("record_result")
    if rr is not None and not rr.get("ok"):
        # U16: evidence that did not reach state.json is evidence that does
        # not exist - the bb-buck failure mode, made loud.
        print(f"gate {args.gate}: result NOT recorded in state.json - "
              f"{rr.get('reason')}", file=sys.stderr)
        return 2
    cr = result.get("commit_result")
    if cr is not None and not cr.get("ok"):
        # C4: a requested commit that did not occur is an OPERATIONAL error,
        # even on gate pass - a caller keying on exit 0 must not believe the
        # gate artifacts were preserved when they were not.
        print(f"gate {args.gate}: requested commit did not occur - "
              f"{cr.get('reason')}", file=sys.stderr)
        return 2
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
