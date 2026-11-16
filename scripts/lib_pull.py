#!/usr/bin/env python
"""lib_pull.py - pull a part's KiCad symbol/footprint/3D via easyeda2kicad and
register it in the project library tables (SPEC.md P3, 6.1).

Wraps `easyeda2kicad --full --lcsc_id=Cxxxx --output <base>`, which writes
  <base>.kicad_sym            (symbol library)
  <base>.pretty/<fp>.kicad_mod (footprints, LEGACY (module ...) format)
  <base>.3dshapes/<name>.{wrl,step}
then appends the libraries to <project>/fp-lib-table + sym-lib-table (idempotent).
A part already in the library is left as it is (its footprint repairs stay)
unless --overwrite; only the 3D models its footprint names and <base>.3dshapes
lacks are fetched (easyeda2kicad --3d), which is how a fresh clone, whose
gitignored 3dshapes are gone, gets them back.
Footprints load in KiCad 10 despite the legacy format (LEARNINGS 2026-07-22
[easyeda2kicad]); --verify-load confirms it with `kicad-cli fp export svg`.

A raw pull is NOT fabricable as delivered, so four repairs run by default before
the pull is reported (all idempotent, all text surgery):
  model fit       EasyEDA's 3D model off its own footprint (the TYPE-C-31-M-12
                  USB-C 2.15 mm off and turned, the FPC-05F-20PH20 0.45 mm
                  off): each footprint's model is laid against its pads
                  (check_model_fit's fit: leads on the SMD pads, pins in the
                  holes, every slide within 5 mm at each quarter turn) and its
                  (offset)/(rotate) - and a (scale) not 1 1 1, or a model
                  floating off the board - rewritten to the fit, so render,
                  STEP and the enclosure fitted to it show the part where it
                  is. A model no slide or turn fits is left and listed in
                  model_fit.left                        -> --no-model-fit to skip
  fpfix           silk artifacts on pad copper, outlines under the silk-to-copper
                  bar, plated locating pegs (zero annular ring), legend text
                  hidden under the body, courtyards drawn inside their own pads
                  (EasyEDA's 0603s), an exposed pad's unnumbered thermal vias,
                  a placement type (attr) its pads contradict (EasyEDA's
                  XR0850 inductor: through_hole on two SMD pads)
                                             -> --no-autofix to skip
                  It also reads each part's 3D model and lists in
                  autofix.flags what it can't fix: a model off its footprint,
                  a peg with no hole, a body outside the courtyard.
  lib_refdes_norm every reference text sits at a blanket (0,-4.0) mm whatever the
                  part size                  -> --no-refdes-norm to skip
  shared names    EasyEDA names footprints by package, so a pull can rewrite
                  another part's footprint file (#412): same pads -> the old,
                  repaired file goes back; different pads -> this part's is
                  written as <name>_<LCSC> and its symbol pointed there
                  (result: shared_footprint). Pulled pins numbered twice
                  (USB-A shell legs "0","0", #393) get their own numbers in
                  symbol and footprint when the counts match
                  (result: renumbered_pins). lib/pullfix.py.
  lib_pin_types   every pin `unspecified`: the symbols this run pulled are
                  retyped (power_in on an IC's VCC/GND-named pins, else
                  passive; from parts/<lcsc>.json extracts beside --parts when
                  there are any)            -> --no-pin-types to skip
A symbol drawn as two or more units (a dual op-amp, a quad gate) is flagged,
not changed: its result gets multi_unit (the unit count) and a warning, and
the payload's multi_unit lists each with its pins by unit (#378). Place every
unit; schlib's Sheet.add_component does.
Every footprint's (model ...) path is then rewritten from the absolute path
easyeda2kicad writes to ${KIPRJMOD}/../lib/<lib>.3dshapes/<name> (relative to
--project, else <out_dir>/../kicad - for an out dir named lib/ even before
that kicad/ exists, the usual order of a new board), so the board survives
its checkout moving.
`--verify-drc` measures the result the only way that counts: one instance of each
pulled footprint alone on a scratch board, real DRC (LEARNINGS 2026-07-28
[easyeda2kicad][drc] - geometry checkers and DRC disagree).

Per-part success is judged from the SYMBOL LIBRARY, the only ground truth
(LEARNINGS 2026-07-28 [easyeda2kicad][parts]): a part is present iff
aiee.kicad_sym holds a top-level (symbol ...) block carrying
(property "LCSC Part" "<id>") whose Footprint property resolves to a file in
aiee.pretty. Footprints are SHARED (one C0603 served 5 parts) and record only
the first puller's id, so footprint grep is NOT a per-part signal.

A run keeps to a time budget (--budget-s, default 7 min): an EasyEDA call
that would overrun it isn't started, the rest come back `deferred` with
status `partial` (exit 1), and the fix-ups below still run on what was
pulled. Re-running the same command resumes: parts already in the library
aren't fetched again. Every easyeda2kicad call has a timeout (90 s, or
HWDE_EASYEDA_TIMEOUT_S, cut to what is left of the budget) that kills its
whole process group, so a stalled EasyEDA request can't hang the run: that
part comes back `deferred` (`stalled`) and the batch goes on. Each call
prints one progress line on stderr, and with --out the report is rewritten
(status `running`) after every part, so a killed run leaves a record.

`--parts parts/parts.json` pulls every part in ONE paced batch: parts are
spaced --pace-s apart (auto: 0 for <=10 parts, 15 s for more - EasyEDA's
CloudFront WAF tripped at 4/35s after ~13 parts while 1/15s ran 31 clean), a
403/Forbidden failure backs off 90 s and retries once, and duplicate symbol
blocks (easyeda2kicad re-appends any name with a space or '/') are de-duped
after the batch.

Contract (SPEC section 6): argparse, JSON to stdout or --out, exit 0/1/2.
  0 = every requested part pulled (or already present)
  1 = one or more parts failed to pull, or were deferred (re-run to resume)
  2 = internal error / bad arguments

Examples:
  lib_pull.py --lcsc C1525
  lib_pull.py --parts parts/parts.json --project board/kicad --verify-load
  lib_pull.py --lcsc C8734 C25804 --out-dir board/lib --project board/kicad --verify-load
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_model_fit  # noqa: E402
import fplib  # noqa: E402
import fpfix  # noqa: E402
import pullfix  # noqa: E402
import kc  # noqa: E402
import lib_pin_types  # noqa: E402
import lib_refdes_norm  # noqa: E402
from lib import env  # noqa: E402


def _call_timeout_s() -> float:
    """The longest one easyeda2kicad call may take (HWDE_EASYEDA_TIMEOUT_S)."""
    try:
        return max(5.0, float(os.environ.get("HWDE_EASYEDA_TIMEOUT_S", "")))
    except ValueError:
        return CALL_TIMEOUT_S


def _run_bounded(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, capture_output) that really ends at timeout_s.

    subprocess.run kills only its child on a timeout and then waits for the
    pipes to close, so a grandchild still holding them (or a child stuck in a
    socket read that ignores the kill's delivery until it returns) makes it
    wait on. This runs the command in its own session, kills the whole
    process group, and stops reading the pipes after a short grace."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise subprocess.TimeoutExpired(cmd, timeout_s) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _run_easyeda(lcsc: str, base: Path, no_3d: bool, overwrite: bool,
                 only_3d: bool = False,
                 timeout_s: float | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "easyeda2kicad",
           f"--lcsc_id={lcsc}", "--output", str(base)]
    if only_3d:
        cmd.append("--3d")
    else:
        cmd += ["--symbol", "--footprint"] if no_3d else ["--full"]
    if overwrite:
        cmd.append("--overwrite")
    return _run_bounded(cmd, timeout_s or _call_timeout_s())


def _stalled(lcsc: str, timeout_s: float | None) -> dict:
    """A call that gave nothing in its time: EasyEDA or the network stalled,
    not the part. Deferred, so the run says partial and a re-run resumes."""
    t = timeout_s or _call_timeout_s()
    return {"lcsc": lcsc, "status": "deferred", "stalled": True,
            "detail": f"easyeda2kicad stalled: no answer in {t:.0f} s "
                      "(EasyEDA or the network); re-run to resume"}


def _progress(msg: str) -> None:
    """One line per EasyEDA call on stderr, so a slow run shows it is alive
    (stdout stays the JSON report)."""
    print(f"lib_pull: {msg}", file=sys.stderr, flush=True)


def _symbol_entry(sym_lib: Path, lcsc: str) -> dict | None:
    """The symbol-index entry for this exact LCSC id, or None."""
    for entry in fplib.symbol_index(sym_lib):
        if entry["lcsc"] == lcsc:
            return entry
    return None


def _present(lcsc: str, base: Path) -> tuple[dict | None, Path | None]:
    """(symbol-index entry, footprint file) when this part is in the library."""
    entry = _symbol_entry(base.with_suffix(".kicad_sym"), lcsc)
    if entry is None or not entry["footprint"]:
        return entry, None
    cand = (Path(str(base) + ".pretty")
            / (entry["footprint"].split(":")[-1] + ".kicad_mod"))
    return entry, (cand if cand.exists() else None)


def _pull_one(lcsc: str, base: Path, no_3d: bool, overwrite: bool,
              timeout_s: float | None = None) -> dict:
    sym_lib = base.with_suffix(".kicad_sym")
    pretty = Path(str(base) + ".pretty")
    # A part already in the library is not handed to easyeda2kicad at all: it
    # stops at the existing symbol, so it fetches no 3D model (that is
    # _restore_models' job), and a symbol whose name has a space or '/' is
    # appended again anyway (_dedupe_symbols).
    entry, fp_file = (None, None) if overwrite else _present(lcsc, base)
    before: dict[str, str] = {}
    if entry is not None and fp_file is not None:
        cp, log, already, created = None, "", True, False
    else:
        before = _fp_snapshot(pretty)
        try:
            if timeout_s is None:
                cp = _run_easyeda(lcsc, base, no_3d, overwrite)
            else:
                cp = _run_easyeda(lcsc, base, no_3d, overwrite,
                                  timeout_s=timeout_s)
        except subprocess.TimeoutExpired:
            return _stalled(lcsc, timeout_s)
        log = (cp.stdout or "") + (cp.stderr or "")
        already = "already exists" in log
        created = "Created Kicad footprint" in log or "Created Kicad symbol" in log
    # Success must be judged PER PART, from the filesystem, through the SYMBOL
    # library (LEARNINGS 2026-07-28 [easyeda2kicad][parts]): a part is present
    # iff aiee.kicad_sym holds a top-level (symbol ...) block with
    # (property "LCSC Part" "<id>") whose Footprint property resolves in
    # aiee.pretty. The previous footprint-grep gate failed BOTH ways: a
    # successful pull sharing an existing footprint (the file records only the
    # first puller's id) reported "error", and a 403'd symbol pull whose id
    # happened to sit in some footprint file reported "pulled" (and C2580
    # substring-matched C25804's file).
    if cp is not None:
        entry, fp_file = _present(lcsc, base)
    if entry is None or fp_file is None:
        if entry is None:
            why = (f'no symbol carrying (property "LCSC Part" "{lcsc}") '
                   f"in {sym_lib.name}")
        else:
            why = (f"symbol '{entry['name']}' names footprint "
                   f"'{entry['footprint']}' but no such file exists in {pretty.name}")
        return {"lcsc": lcsc, "status": "error",
                "symbol_verified": entry is not None,
                "footprint_verified": False,
                "detail": why + (" | log: " + log.strip()[-300:] if log.strip()
                                 else f" (easyeda2kicad rc={cp.returncode})")}

    warnings = []
    fp_reports = []
    shared = renumbered = None
    if cp is not None:
        fp_file, shared = _shared_footprint(lcsc, base, entry, fp_file,
                                            before, overwrite, warnings)
        renumbered = _duplicate_pins(base, entry, fp_file, warnings)
    try:
        f = fplib.parse_footprint(fp_file)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not parse {fp_file.name}: {exc}")
    else:
        if not f.has_courtyard:
            warnings.append(f"{f.name}: no courtyard layer (easyeda2kicad "
                            "footprints sometimes omit it; DRC courtyard checks degrade)")
        fp_reports.append({
            "name": f.name,
            "file": str(fp_file),
            "copper_pads": len(f.copper_pads),
            "courtyard": f.has_courtyard,
            "silk": f.has_layer_kind("SilkS"),
        })
    return {
        "lcsc": lcsc,
        "status": "exists" if (already and not created) else "pulled",
        "symbol": entry["name"],
        "symbol_verified": True,
        "footprint_verified": True,
        "symbol_lib": str(sym_lib) if sym_lib.exists() else None,
        "footprint_lib": str(pretty) if pretty.is_dir() else None,
        "footprints": fp_reports,
        "warnings": warnings,
        **({"shared_footprint": shared} if shared else {}),
        **({"renumbered_pins": renumbered} if renumbered else {}),
    }


def _fp_snapshot(pretty: Path) -> dict[str, str]:
    """name -> text of every footprint in the library before a pull."""
    if not pretty.is_dir():
        return {}
    out = {}
    for p in pretty.glob("*.kicad_mod"):
        try:
            out[p.name] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
    return out


def _shared_footprint(lcsc: str, base: Path, entry: dict, fp_file: Path,
                      before: dict[str, str], overwrite: bool,
                      warnings: list[str]) -> tuple[Path, dict | None]:
    """The pull rewrote a footprint file that was already there (#412):
    EasyEDA names footprints by package, so another part may own it.
    Same copper -> the old file goes back (it carries fpfix's repairs),
    unless --overwrite asked for a fresh pull. Different copper and another
    part's symbol names the file -> the old file goes back and this part's
    footprint is written as <name>_<lcsc>, its symbol pointed there.
    -> (this part's footprint file, what was done or None)."""
    old = before.get(fp_file.name)
    if old is None:
        return fp_file, None
    try:
        new = fp_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return fp_file, None
    if new == old:
        return fp_file, None
    sym_lib = base.with_suffix(".kicad_sym")
    stem = fp_file.stem
    others = sorted({e["lcsc"] or e["name"] for e in fplib.symbol_index(sym_lib)
                     if e["footprint"].split(":")[-1] == stem
                     and e["lcsc"] != lcsc})
    try:
        same = pullfix.same_pads(old, new)
    except Exception as exc:  # noqa: BLE001 - unparseable: keep what was there
        warnings.append(f"{fp_file.name}: could not compare the re-pulled "
                        f"footprint ({exc}); the earlier file is kept")
        same = True
    if same:
        if overwrite and not others:
            return fp_file, None
        fp_file.write_text(old, encoding="utf-8")
        return fp_file, {"footprint": stem, "action": "kept_existing",
                         "shared_with": others}
    if not others:
        return fp_file, None
    split = f"{stem}_{lcsc}"
    target = fp_file.with_name(split + ".kicad_mod")
    text = sym_lib.read_text(encoding="utf-8")
    lib = entry["footprint"].split(":")[0] if ":" in entry["footprint"] \
        else base.name
    out = pullfix.set_symbol_footprint(text, entry["name"], f"{lib}:{split}")
    if out is None:
        warnings.append(f"{fp_file.name}: shared with {others} and its pads "
                        f"differ, but symbol {entry['name']} has no Footprint "
                        f"property to re-point; left as pulled")
        return fp_file, {"footprint": stem, "action": "conflict",
                         "shared_with": others}
    target.write_text(pullfix.rename_footprint(new, split), encoding="utf-8")
    fp_file.write_text(old, encoding="utf-8")
    sym_lib.write_text(out, encoding="utf-8")
    entry["footprint"] = f"{lib}:{split}"
    warnings.append(f"{stem} is another part's footprint ({', '.join(others)}) "
                    f"with different pads: this part's is {split}")
    return target, {"footprint": stem, "action": "split", "as": split,
                    "shared_with": others}


def _duplicate_pins(base: Path, entry: dict, fp_file: Path,
                    warnings: list[str]) -> list[dict] | None:
    """Give duplicate pin/pad numbers (EasyEDA's USB-A shell legs, both "0")
    their own numbers in the symbol and the footprint (#393)."""
    sym_lib = base.with_suffix(".kicad_sym")
    try:
        sym = sym_lib.read_text(encoding="utf-8")
        fp = fp_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    sym2, fp2, changes, warns = pullfix.renumber_duplicate_pins(
        sym, entry["name"], fp)
    warnings.extend(warns)
    if not changes:
        return None
    others = [e["name"] for e in fplib.symbol_index(sym_lib)
              if e["footprint"].split(":")[-1] == fp_file.stem
              and e["name"] != entry["name"]]
    if others:
        warnings.append(f"{entry['name']}: duplicate pin numbers left: its "
                        f"footprint {fp_file.stem} is shared with {others}")
        return None
    sym_lib.write_text(sym2, encoding="utf-8")
    fp_file.write_text(fp2, encoding="utf-8")
    warnings.append(f"{entry['name']}: duplicate pin numbers renumbered "
                    + ", ".join(f"{c['from']}->{c['to']}" for c in changes))
    return changes


# ------------------------------------------------------- batch pacing + dedupe

# Seconds of pulling per run by default: a 10-minute tool call keeps a few
# minutes for the fix-ups, the load check and the report. 21 parts at the
# 15 s pace once took over 10 minutes; killed, the run left every footprint's
# 3D path absolute (hifi-receiver-2 lite-main, snag 5).
DEFAULT_BUDGET_S = 420.0
# An EasyEDA call takes about this long before any have been timed.
EST_CALL_S = 20.0
# The 403 back-off: CloudFront's WAF block clears in ~60-120 s.
BACKOFF_S = 90.0
# The longest one easyeda2kicad call may take: a clean pull with its 3D model
# takes 3-20 s, so a call silent for this long has stalled (issue #410).
# HWDE_EASYEDA_TIMEOUT_S overrides it. A call never runs past the budget.
CALL_TIMEOUT_S = 90.0
# A call isn't started with less than this left of the budget.
MIN_CALL_S = 10.0


class _Pacer:
    """Spaces the EasyEDA calls of one run pace_s apart, whichever step makes
    them (the WAF counts requests, not parts), and keeps them to the run's
    time budget: fits() says whether one more call (after its pace and any
    extra wait) would still end inside budget_s."""

    def __init__(self, pace_s: float, budget_s: float | None = None):
        self.pace_s = pace_s
        self.budget_s = budget_s or None
        self.calls = 0
        self.slept = 0.0
        self.t0 = time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)
        self.slept += s

    def wait(self) -> None:
        if self.calls and self.pace_s > 0:
            self.sleep(self.pace_s)
        self.calls += 1

    def fits(self, extra: float = 0.0) -> bool:
        if not self.budget_s:
            return True
        spent = time.monotonic() - self.t0
        est = (spent - self.slept) / self.calls if self.calls else EST_CALL_S
        pace = self.pace_s if self.calls else 0.0
        return spent + pace + extra + max(est, MIN_CALL_S) <= self.budget_s

    def call_timeout(self) -> float:
        """The timeout for the call about to start: the per-call cap, cut to
        what is left of the budget, so no call outlives the run's budget."""
        cap = _call_timeout_s()
        if not self.budget_s:
            return cap
        left = self.budget_s - (time.monotonic() - self.t0)
        return max(MIN_CALL_S, min(cap, left))


def _auto_pace(n_parts: int) -> float:
    """0 s for small pulls, 15 s otherwise (the measured WAF budget: 4/35s
    tripped CloudFront after ~13 parts; 1/15s ran 31 parts clean)."""
    return 0.0 if n_parts <= 10 else 15.0


def _is_rate_limited(detail: str) -> bool:
    return "403" in detail or "Forbidden" in detail


def _pull_all(ids: list[str], base: Path, no_3d: bool, overwrite: bool,
              pacer: _Pacer, on_result=None) -> tuple[list[dict], int]:
    """Pull every id, paced; on a 403 back off 90 s and retry that part once.

    The retried part is re-verified on disk by _pull_one's symbol-index gate
    before it can report success. A call (or back-off) that would run past
    the pacer's budget isn't started: that part and the rest come back
    `deferred`, so the run ends with time left to fix up what it has, and a
    re-run resumes (parts in the library aren't fetched again).
    on_result(results) runs after each part, so a run killed mid-batch
    still leaves a record of what it pulled (#412).
    Returns (results, n_retried).
    """
    results: list[dict] = []
    retried = 0
    for i, lcsc in enumerate(ids):
        timeout_s = None
        if overwrite or None in _present(lcsc, base):
            if not pacer.fits():
                results += [{"lcsc": l, "status": "deferred",
                             "detail": "not started: the run's time budget"}
                            for l in ids[i:]]
                break
            pacer.wait()
            timeout_s = pacer.call_timeout()
        t0 = time.monotonic()
        r = _pull_one(lcsc, base, no_3d, overwrite, timeout_s=timeout_s)
        if timeout_s is not None:
            _progress(f"[{i + 1}/{len(ids)}] {lcsc} {r['status']} "
                      f"({time.monotonic() - t0:.1f} s)")
        if r["status"] == "error" and _is_rate_limited(r.get("detail", "")):
            if not pacer.fits(BACKOFF_S):
                results.append(dict(r, status="deferred",
                                    detail="rate-limited, and a back-off would "
                                           "overrun the time budget"))
                results += [{"lcsc": l, "status": "deferred",
                             "detail": "not started: the run's time budget"}
                            for l in ids[i + 1:]]
                break
            pacer.sleep(BACKOFF_S)   # CloudFront WAF block clears in ~60-120 s
            r = _pull_one(lcsc, base, no_3d, overwrite,
                          timeout_s=pacer.call_timeout())
            r["retried"] = True
            retried += 1
        results.append(r)
        if on_result is not None:
            on_result(results)
    return results, retried


_MODEL = re.compile(r'\(model\s+"?([^"\s)]+)"?')


def _missing_models(fp_file: Path, models: Path) -> list[str]:
    """Model files this footprint names in <lib>.3dshapes that are not there.

    Only paths into a directory named like `models` count: a footprint that
    points at KiCad's own 3D library is not this library's to fill."""
    try:
        text = fp_file.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in _MODEL.findall(text):
        name = raw.replace("\\", "/").rsplit("/", 1)
        if len(name) == 2 and name[0].rsplit("/", 1)[-1] == models.name \
                and not (models / name[1]).exists():
            out.append(name[1])
    return sorted(set(out))


def _restore_models(results: list[dict], base: Path, pacer: _Pacer) -> dict:
    """Fetch the 3D models a part's footprint names but the library lacks.

    The 3D models are gitignored (manufacturers' material, NOTICE), so a fresh
    clone has every part in the library and none of its models; easyeda2kicad
    stops at the existing symbol and fetches none, and --overwrite would undo
    the footprint repairs. This runs easyeda2kicad --3d alone, which writes
    only <lib>.3dshapes. A shared footprint is restored once: the parts after
    the first find its model present. A part whose model has another name than
    the footprint names (a shared footprint pulled first by another part) is
    left in `still_missing`, a warning, not a failure."""
    models = Path(str(base) + ".3dshapes")
    restored: list[str] = []
    still: list[str] = []
    for r in results:
        if r["status"] in ("error", "deferred"):
            continue
        for fp in r.get("footprints", []):
            missing = _missing_models(Path(fp["file"]), models)
            if not missing:
                continue
            if not pacer.fits():
                still += missing
                r.setdefault("warnings", []).append(
                    f"{fp['name']}: 3D model(s) {', '.join(missing)} not "
                    "fetched: the run's time budget (re-run to resume)")
                continue
            pacer.wait()
            try:
                cp = _run_easyeda(r["lcsc"], base, False, False, only_3d=True,
                                  timeout_s=pacer.call_timeout())
                log = (cp.stdout or "") + (cp.stderr or "")
            except subprocess.TimeoutExpired:
                log = "easyeda2kicad stalled (EasyEDA or the network)"
            _progress(f"3D {r['lcsc']} {fp['name']}")
            left = _missing_models(Path(fp["file"]), models)
            got = sorted(set(missing) - set(left))
            restored += got
            fp["models_restored"] = got
            if left:
                still += left
                r.setdefault("warnings", []).append(
                    f"{fp['name']}: 3D model(s) {', '.join(left)} still missing"
                    + (" | log: " + log.strip()[-200:] if log.strip() else ""))
    return {"restored": sorted(set(restored)),
            "still_missing": sorted(set(still))}


_SYM_NAME = re.compile(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"')


def _dedupe_symbols(sym_lib: Path) -> dict:
    """Drop duplicate top-level (symbol "NAME") blocks, keeping the first.

    easyeda2kicad's dedup guard checks the RAW EasyEDA name but writes the
    sanitized one, so any part named with a space or '/' is APPENDED AGAIN on
    every re-pull (LEARNINGS 2026-07-28: an 18-symbol lib became 21). Paren
    scanner (fpfix.top_level_nodes), not a parse: only duplicate blocks are
    removed, nothing else is reformatted.
    """
    if not sym_lib.exists():
        return {"removed": 0}
    try:
        text = sym_lib.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # never round-trip a lib we could not decode losslessly
        return {"removed": 0, "skipped": f"{type(exc).__name__}: {exc}"}
    seen: set[str] = set()
    drops: list[tuple[int, int]] = []
    names: list[str] = []
    for head, s, e in fpfix.top_level_nodes(text):
        if head != "symbol":
            continue
        m = _SYM_NAME.match(text[s:e])
        if not m:
            continue
        name = m.group(1)
        if name in seen:
            drops.append((s, e))
            names.append(name)
        else:
            seen.add(name)
    if drops:
        out = text
        for s, e in sorted(drops, key=lambda t: -t[0]):
            ls = out.rfind("\n", 0, s) + 1
            le = e
            if out[le:le + 1] == "\n":
                le += 1
            if out[ls:s].strip():
                ls, le = s, e
            elif ls and not out[out.rfind("\n", 0, ls - 1) + 1:ls].strip():
                # easyeda2kicad inserts each symbol after a blank line
                ls = out.rfind("\n", 0, ls - 1) + 1
            out = out[:ls] + out[le:]
        sym_lib.write_text(out, encoding="utf-8")
    return {"removed": len(drops), "names": sorted(set(names))}


# ---------------------------------------------------------- lib-table registration

def _uri_for(target: Path, project: Path | None) -> str:
    """A KiCad lib-table URI: ${KIPRJMOD}-relative (portable) when possible.

    Uses os.path.relpath so a lib SIBLING of the project dir resolves to
    ${KIPRJMOD}/../lib/... (KiCad expands the .. fine). Falls back to an absolute
    path only across drives (relpath raises ValueError on win32).
    """
    import os
    t = target.resolve()
    if project is not None:
        try:
            rel = os.path.relpath(t, project.resolve())
            return "${KIPRJMOD}/" + Path(rel).as_posix()
        except ValueError:
            pass
    return t.as_posix()


def _register(table_path: Path, table_head: str, nickname: str, uri: str) -> str:
    """Append a lib entry to an fp-/sym-lib-table (create if absent, idempotent).

    Returns "added" | "present" | "created".
    """
    line = (f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
            f'(options "")(descr ""))')
    if not table_path.exists():
        table_path.parent.mkdir(parents=True, exist_ok=True)
        table_path.write_text(f"({table_head}\n  (version 7)\n{line}\n)\n",
                              encoding="utf-8")
        return "created"
    text = table_path.read_text(encoding="utf-8", errors="replace")
    if f'(name "{nickname}")' in text:
        return "present"
    idx = text.rstrip().rfind(")")
    if idx < 0:  # malformed - rewrite fresh
        table_path.write_text(f"({table_head}\n  (version 7)\n{line}\n)\n",
                              encoding="utf-8")
        return "created"
    new = text[:idx].rstrip("\n") + "\n" + line + "\n" + text[idx:]
    table_path.write_text(new, encoding="utf-8")
    return "added"


def _register_project(project: Path, nickname: str, sym_lib: Path | None,
                      pretty: Path | None) -> dict:
    project.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    if pretty is not None and pretty.is_dir():
        out["fp"] = _register(project / "fp-lib-table", "fp_lib_table",
                              nickname, _uri_for(pretty, project))
    if sym_lib is not None and sym_lib.exists():
        out["sym"] = _register(project / "sym-lib-table", "sym_lib_table",
                               nickname, _uri_for(sym_lib, project))
    return out


# ------------------------------------------------------------ post-pull hygiene

def _autofix(pretty: Path, names: list[str], dry_run: bool) -> dict:
    """Sanitise the footprints this pull produced (fpfix rules A-I).

    Every pull ships the same library-inherent defects - silk artifacts on pad
    copper, outlines under the silk-to-copper bar, plated locating pegs with no
    annular ring, legend text hidden under the body, a courtyard that is the
    body outline and leaves the pads outside, a placement type (attr) the
    pads contradict, an edge-mount part's open board-edge line on Edge.Cuts
    (moved to Dwgs.User once the model fit has faced the model on it). They were repaired by hand
    on three shipped boards (boards/*/lib/EDITS.md) before this ran at pull time.
    """
    rep = fpfix.fix_lib(pretty, dry_run=dry_run, names=names)
    return {"footprints": rep["footprints"], "changed": rep["changed"],
            "actions": rep["actions"], "residue": rep["residue"],
            # what no rule can fix: the part's drawing has to settle it
            "flags": [dict(f, footprint=r["footprint"]) for r in rep["results"]
                      for f in r.get("flags", [])],
            "results": [r for r in rep["results"]
                        if r["changed"] or r["residue"] or r.get("flags")]}


def _model_fit(pretty: Path, names: list[str], dry_run: bool) -> dict:
    """Fit each pulled footprint's 3D model to its pads (check_model_fit).

    EasyEDA draws a part's model in its own frame, and for some parts that
    frame is not the footprint's: the TYPE-C-31-M-12 USB-C model sat 2.15 mm
    off and turned on lite-main, the FPC-05F-20PH20's 0.45 mm off on the
    core board, and each was found on a board and repaired by hand. Runs
    after the models are restored (a fresh clone's are fetched first) and
    before fpfix, whose model flags then read the fitted model. An
    edge-launch part's model is also faced and seated on the board edge its
    footprint marks on Edge.Cuts ("edge": turned, slide_mm; #405), before
    fpfix rule I moves that mark to Dwgs.User."""
    rep = check_model_fit.fit_lib(pretty, names, dry_run=dry_run)
    rep["fixed"] = [{k: m[k] for k in ("footprint", "model", "fixed", "from",
                                         "to", "edge") if k in m}
                    for m in rep["fixed"]]
    return rep


def _refdes_norm(pretty: Path, dry_run: bool) -> dict:
    """Re-derive every refdes offset from the footprint's own geometry.

    easyeda2kicad parks EVERY reference at a blanket (0, -4.0) mm whatever the
    part size, so a populated board cannot be read (LEARNINGS 2026-07-29
    [parts][silk]). Must run AFTER the silk fix - it measures against the silk
    that survives - and BEFORE board_init, because once footprints are placed
    the offsets are copied into the .kicad_pcb.
    """
    rows = [lib_refdes_norm.normalize(f, 0.25, 1.0, dry_run)
            for f in sorted(Path(pretty).glob("*.kicad_mod"))]
    return {"footprints": len(rows),
            "changed": sum(1 for r in rows if r["status"] == "changed"),
            "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
            "skipped": sum(1 for r in rows if r["status"] == "skipped"),
            "results": [r for r in rows if r["status"] == "changed"]}


def _pin_types(sym_lib: Path, results: list[dict], parts_file: str | None) -> dict:
    """Retype the pins of the symbols this run pulled (lib_pin_types).

    Only "pulled" symbols: one already in the library may carry types set from
    a datasheet extract since, which a re-run without it would revert."""
    names = {r["symbol"] for r in results if r["status"] == "pulled"}
    if not names or not sym_lib.exists():
        return {"changed": 0, "symbols": sorted(names)}
    extracts = {}
    if parts_file:
        extracts = lib_pin_types._load_extracts(
            [str(Path(parts_file).parent / "C*.json")])
    rules, matched = lib_pin_types.build_rules(fplib.symbol_index(sym_lib), extracts)
    text = sym_lib.read_text(encoding="utf-8")
    new, changes = lib_pin_types.retype(text, rules, names)
    if changes:
        sym_lib.write_text(new, encoding="utf-8")
    return {"changed": len(changes), "symbols": sorted(names),
            "extracts_matched": sorted(matched),
            "power_in": sorted({f"{c['symbol']}:{c['pin']}" for c in changes
                                if c["to"] == "power_in"})}


def _multi_unit(sym_lib: Path, results: list[dict]) -> list[dict]:
    """Flag each pulled (or present) symbol drawn as two or more units (#378).

    A board generator that places only unit 1 of a dual op-amp or a quad
    gate leaves the other units' pins out of the netlist: KiCad's ERC says
    missing_unit, and the footprint's pads for them float. schlib's
    Sheet.add_component places every unit; a hand-built sheet must too."""
    units = fplib.symbol_units(sym_lib) if sym_lib.exists() else {}
    out = []
    for r in results:
        u = units.get(r.get("symbol") or "")
        if r["status"] not in ("pulled", "exists") or not u:
            continue
        n = len([k for k in u if k > 0])
        r["multi_unit"] = n
        r.setdefault("warnings", []).append(
            f"{r['symbol']}: {n} units - place every unit (schlib "
            f"Sheet.add_component does), or ERC reports missing_unit and "
            f"those pins float")
        out.append({"symbol": r["symbol"], "lcsc": r["lcsc"], "units": n,
                    "pins": {str(k): v for k, v in sorted(u.items())}})
    return out


def _portable_models(pretty: Path, models: Path, project: Path | None,
                     explicit: bool = False) -> dict:
    """Point every footprint's (model ...) at ${KIPRJMOD}/.. instead of the
    absolute path easyeda2kicad writes (out_dir is resolved before the pull,
    LEARNINGS 2026-07-28). An absolute path names the checkout that ran the
    pull and dies with it - eleven boards' models pointed into removed track
    worktrees. The project dir is --project, else <out_dir>/../kicad, the
    workspace layout; with neither the paths stay absolute and `skipped` says
    so, and check_model_paths.py flags the board later. An explicit
    --project need not exist yet: _register_project creates it after this,
    and the rewrite only resolves paths."""
    if project is None or not (explicit or project.is_dir()):
        return {"rewritten": 0, "skipped": "no --project and no kicad/ dir "
                f"beside {pretty.parent} - model paths left absolute"}
    if not models.is_dir():
        return {"rewritten": 0, "skipped": f"no {models.name} (--no-3d?)"}
    proj = project.resolve()
    changed = {}
    for f in sorted(pretty.glob("*.kicad_mod")):
        text = f.read_text(encoding="utf-8")
        new, done = kc.model_paths_portable(text, proj, pretty.parent.parent,
                                            [models])
        if done:
            f.write_text(new, encoding="utf-8")
            changed[f.name] = sorted(set(done.values()))
    return {"rewritten": len(changed), "footprints": changed}


# ---------------------------------------------------------- optional load check

def _verify_load(pretty: Path) -> dict:
    """Confirm KiCad itself parses the pulled footprints (fp export svg)."""
    try:
        cli = env.find_kicad_cli()
    except env.EnvError as exc:
        return {"ok": False, "detail": str(exc)}
    if cli is None:
        return {"ok": False, "detail": "no kicad-cli found"}
    with tempfile.TemporaryDirectory() as td:
        outdir = Path(td) / "svg"
        outdir.mkdir(parents=True, exist_ok=True)  # dir must pre-exist (LEARNINGS)
        try:
            cp = subprocess.run([str(cli), "fp", "export", "svg", str(pretty),
                                 "--output", str(outdir)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": "kicad-cli fp export svg timed out"}
        svgs = list(outdir.glob("*.svg"))
        err = "Error creating svg" in (cp.stdout + cp.stderr)
        return {"ok": bool(svgs) and not err,
                "svgs": len(svgs),
                "detail": (cp.stdout + cp.stderr).strip()[-300:] if not svgs else "ok"}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lcsc", nargs="+", help="LCSC id(s), e.g. C1525")
    ap.add_argument("--parts", help="parts/parts.json - pull every parts[].lcsc "
                    "in one paced batch (alternative to --lcsc)")
    ap.add_argument("--pace-s", type=float, default=None,
                    help="seconds between parts (default auto: 0 for <=10 "
                         "parts, 15 for more - the EasyEDA WAF budget)")
    ap.add_argument("--out-dir", default=None,
                    help="library directory. Default: <project>/../lib when "
                         "--project is given, else ./lib")
    ap.add_argument("--lib-name", default="aiee", help="library nickname/base (default aiee)")
    ap.add_argument("--project", help="project dir to register the libs into")
    ap.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S,
                    help="stop starting EasyEDA calls once this many seconds "
                         "would be spent (default %(default)s, inside a "
                         "10-minute tool call with time for the fix-ups); the "
                         "rest come back deferred and a re-run resumes. "
                         "0 = no budget")
    ap.add_argument("--no-3d", action="store_true", help="skip 3D models (faster)")
    ap.add_argument("--overwrite", action="store_true", help="re-pull existing parts")
    ap.add_argument("--verify-load", action="store_true",
                    help="confirm footprints load via kicad-cli fp export svg")
    ap.add_argument("--verify-drc", action="store_true",
                    help="DRC the pulled footprints alone on a scratch board")
    ap.add_argument("--no-autofix", action="store_true",
                    help="skip the fpfix silk/peg/legend sanitiser (NOT advised: "
                         "every pull ships those defects)")
    ap.add_argument("--no-model-fit", action="store_true",
                    help="leave each footprint's 3D model where EasyEDA put "
                         "it, even off its pads (NOT advised)")
    ap.add_argument("--no-refdes-norm", action="store_true",
                    help="skip the refdes-offset normalisation")
    ap.add_argument("--no-pin-types", action="store_true",
                    help="leave the pulled symbols' pin types as easyeda2kicad "
                         "wrote them (all unspecified)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)
    if bool(args.lcsc) == bool(args.parts):
        ap.error("exactly one of --lcsc / --parts is required")

    try:
        # A bare relative default resolves against the CWD, and orchestrators run
        # from the repo root - so "lib" silently became <repo>/lib, shared by
        # every concurrent board run, while the board's own lib/ stayed empty
        # and the script still reported pass (LEARNINGS, lumina-strobe
        # BLOCKING-06). Derive from --project instead, and refuse the repo root.
        if args.out_dir:
            out_dir = Path(args.out_dir)
        elif args.project:
            out_dir = Path(args.project).resolve().parent / "lib"
        else:
            out_dir = Path("lib")
        try:
            if out_dir.resolve() == (env.repo_root() / "lib").resolve():
                raise RuntimeError(
                    f"refusing to use the repo-root library {out_dir.resolve()} - "
                    f"it is shared by every board run. Pass --out-dir "
                    f"boards/<board>/lib, or pass --project so the default "
                    f"derives from it.")
        except (OSError, ValueError):
            pass
        out_dir.mkdir(parents=True, exist_ok=True)
        # easyeda2kicad copies the --output string VERBATIM into every
        # footprint's (model ...) path, and KiCad resolves a relative one
        # against the PROJECT dir, where it does not exist - STEP export and 3D
        # render then silently lose every model (LEARNINGS 2026-07-28
        # [easyeda2kicad][parts]). Resolve before handing it over.
        out_dir = out_dir.resolve()
        base = out_dir / args.lib_name

        if args.parts:
            pdata = json.loads(Path(args.parts).read_text(encoding="utf-8"))
            ids: list[str] = []
            for p in pdata.get("parts", []):
                l = (p.get("lcsc") or "").strip()
                if l and l not in ids:
                    ids.append(l)
            if not ids:
                raise RuntimeError(f"{args.parts} has no parts[].lcsc ids")
        else:
            ids = list(args.lcsc)
        pace_s = _auto_pace(len(ids)) if args.pace_s is None else max(0.0, args.pace_s)
        pacer = _Pacer(pace_s, args.budget_s)
        def progress(results):
            # a run killed mid-batch (the tool call's limit) still leaves a
            # record of what it pulled; the fix-ups run at the end
            if args.out:
                Path(args.out).write_text(json.dumps(
                    {"script": "lib_pull", "status": "running",
                     "lib_name": args.lib_name, "results": results},
                    indent=1, ensure_ascii=True), encoding="utf-8")

        results, n_retried = _pull_all(ids, base, args.no_3d, args.overwrite,
                                       pacer, on_result=progress)
        restored = (None if args.no_3d
                    else _restore_models(results, base, pacer))

        sym_lib = base.with_suffix(".kicad_sym")
        pretty = Path(str(base) + ".pretty")
        dedup = _dedupe_symbols(sym_lib)
        pin_types = (None if args.no_pin_types
                     else _pin_types(sym_lib, results, args.parts))
        multi_unit = _multi_unit(sym_lib, results)

        pulled_fps = sorted({fp["name"] for r in results
                             for fp in r.get("footprints", [])})
        autofix = refdes = model_fit = None
        if pulled_fps and pretty.is_dir():
            if not args.no_model_fit:
                model_fit = _model_fit(pretty, pulled_fps, dry_run=False)
            if not args.no_autofix:
                autofix = _autofix(pretty, pulled_fps, dry_run=False)
            if not args.no_refdes_norm:
                refdes = _refdes_norm(pretty, dry_run=False)

        models = None
        if pretty.is_dir():
            project = (Path(args.project) if args.project
                       else out_dir.parent / "kicad")
            # boards/<b>/lib -> boards/<b>/kicad even before kicad/ is made:
            # the library is often pulled before the project (hi-fi core)
            models = _portable_models(pretty, Path(str(base) + ".3dshapes"),
                                      project, explicit=bool(args.project)
                                      or out_dir.name == "lib")

        registered = None
        if args.project:
            registered = _register_project(
                Path(args.project), args.lib_name,
                sym_lib if sym_lib.exists() else None,
                pretty if pretty.is_dir() else None)

        load = _verify_load(pretty) if (args.verify_load and pretty.is_dir()) else None
        drc = None
        if args.verify_drc and pulled_fps and pretty.is_dir():
            d = fpfix.scratch_drc(pretty, pulled_fps)
            drc = {"status": d["status"], "counts": d["counts"],
                   "by_check": d["by_check"], "missing": d["missing"]}

        failed = [r for r in results if r["status"] == "error"]
        deferred = [r["lcsc"] for r in results if r["status"] == "deferred"]
        payload = {
            "script": "lib_pull",
            "status": "fail" if (failed or (drc and drc["counts"]["total"]))
                      else "partial" if deferred else "pass",
            "deferred": deferred,
            "resume": ("re-run the same command: the parts already in the "
                       "library are skipped" if deferred else None),
            "lib_name": args.lib_name,
            "parts_file": args.parts,
            "paced_s": pace_s,
            "retried": n_retried,
            "symbol_lib": str(sym_lib) if sym_lib.exists() else None,
            "footprint_lib": str(pretty) if pretty.is_dir() else None,
            "results": results,
            "symbol_dedup": dedup,
            "registered": registered,
            "model_fit": model_fit,
            "autofix": autofix,
            "refdes_norm": refdes,
            "pin_types": pin_types,
            "multi_unit": multi_unit,
            "model_paths": models,
            "models_restored": restored,
            "load_check": load,
            "drc_check": drc,
        }
    except Exception as exc:  # noqa: BLE001 - contract: any error -> exit 2
        print(json.dumps({"script": "lib_pull", "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    text = json.dumps(payload, indent=1, ensure_ascii=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 1 if payload["status"] in ("fail", "partial") else 0


if __name__ == "__main__":
    sys.exit(main())
