"""benchcorpus - the placement benchmark over the boards corpus (bench.py --corpus).

docs/placement-benchmark.md is its report.  For every board in the boards
repo (env.boards_root()) that has a kicad/*.kicad_pcb, run_board:

  1. copies the board's kicad/ dir to WORK/<board>/kicad/ (the boards repo is
     only read) and deletes earlier route/, route_probe/, route_critical/ and
     anneal/ work dirs from the copy;
  2. records the designer's placement with place_metrics (metrics.orig.json);
  3. strips every track, arc and via through SWIG pcbnew and refuses to go on
     if the saved file still has one (a stale route once sent Freerouting's DSN reader into a
     recursion and voided a whole run).  Zones and locks stay;
  4. place_seed --apply, place_anneal --apply-best, place_metrics on the
     result (metrics.placed.json), then route_auto, all at their defaults.
     route_auto is retried up to three times when the SWIG DSN export dies
     with "wxEntryStart failed";
  5. writes WORK/<board>/result.json the moment the board finishes.

A board whose .kicad_pcb, or whole dir, is gone by the time its turn comes
(renamed in the boards repo mid-run) gets a result.json with only an error, and the run goes on.

Runtime is only a measurement when the box was quiet: a sampler reads the
1-minute load average every few seconds while the board runs, and
runtime_valid is true only when its maximum stayed below LOAD_LIMIT.

run_corpus runs the boards one at a time and skips any board whose
result.json exists unless --rerun names it, so a run that is cut short picks
up where it stopped.  It holds WORK/.lock (flock) so two runs never share a
work dir, starts its own Xvfb on a free display for the SWIG and Freerouting
steps, renices itself (children inherit it), and runs Freerouting headless
(the host JRE lacks libXtst for AWT).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from checklib import CheckError

LOAD_LIMIT = 12.0
STALE_DIRS = ("route", "route_probe", "route_critical", "anneal")
ROUTE_TRIES = 3
STAGE_TIMEOUT_S = 4 * 3600

STRIP_PY = r"""
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
n = 0
for t in list(b.GetTracks()):
    b.Remove(t); n += 1
b.Save(sys.argv[1])
print(f"stripped={n}")
"""
# top-level track items in the saved file; a second SWIG LoadBoard in the same
# process hands back a bare SwigPyObject, so the leftover check reads the text
TRACK_ITEM = re.compile(r"^\t\((segment|arc|via)\s", re.M)


def corpus_boards(root: Path) -> list[Path]:
    """Board dirs under root that have a kicad/*.kicad_pcb, sorted by name."""
    return sorted(d for d in Path(root).iterdir()
                  if d.is_dir() and any((d / "kicad").glob("*.kicad_pcb")))


def result_path(work: Path, name: str) -> Path:
    return Path(work) / name / "result.json"


class LoadSampler:
    """Samples os.getloadavg()[0] on a thread until stop()."""

    def __init__(self, every_s: float = 5.0, read=os.getloadavg):
        self.every_s, self.read, self.samples = every_s, read, []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while True:
            self.samples.append(round(self.read()[0], 2))
            if self._stop.wait(self.every_s):
                return

    def start(self):
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        self._t.join()
        s = self.samples
        return {"max": max(s), "mean": round(sum(s) / len(s), 2),
                "samples": len(s), "limit": LOAD_LIMIT}


def _report(out_json: Path, stdout: str) -> dict:
    """A stage's report: its --out-report file, else its last stdout line."""
    if out_json.is_file():
        return json.loads(out_json.read_text(encoding="utf-8"))
    for line in reversed(stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


def _stage(runner, name, argv, log_dir: Path, env) -> tuple[dict, str]:
    t0 = time.monotonic()
    p = runner(argv, capture_output=True, text=True, env=env,
               timeout=STAGE_TIMEOUT_S)
    (log_dir / f"{name}.out").write_text(p.stdout or "", encoding="utf-8")
    (log_dir / f"{name}.err").write_text(p.stderr or "", encoding="utf-8")
    return ({"exit": p.returncode, "s": round(time.monotonic() - t0, 1)},
            (p.stdout or "") + (p.stderr or ""))


def _placement(report: dict) -> dict:
    m = report.get("metrics") or {}
    return {"status": report.get("status"),
            "violations": (report.get("counts") or {}).get("total"),
            "hpwl_mm": (m.get("hpwl") or {}).get("total_mm"),
            "crossings": (m.get("crossings") or {}).get("count")}


def run_board(board_dir: Path, work: Path, *, scripts: Path, venv_py: str,
              bundled_py: str, env: dict, runner=subprocess.run,
              sampler=None) -> dict:
    """Copy, strip, seed, anneal and route one board; write result.json."""
    name = board_dir.name
    bdir = Path(work) / name
    if bdir.exists():
        shutil.rmtree(bdir)
    try:
        shutil.copytree(board_dir / "kicad", bdir / "kicad")
    except FileNotFoundError:
        pass  # the whole board dir is gone: recorded as lost just below
    for d in STALE_DIRS:
        shutil.rmtree(bdir / "kicad" / d, ignore_errors=True)
    pcbs = sorted((bdir / "kicad").glob("*.kicad_pcb"))
    if not pcbs:
        # the board lost its .kicad_pcb, or its whole dir, after the corpus
        # was listed (a rename in the boards repo mid-run): record it as
        # failed, keep the run going
        bdir.mkdir(parents=True, exist_ok=True)
        return _write_result(work, name, {
            "board": name, "error": f"no kicad/*.kicad_pcb in {board_dir} "
                                    "when its turn came (moved or deleted "
                                    "during the run)"})
    pcb = pcbs[0]
    sampler = (sampler or LoadSampler()).start()
    t0 = time.monotonic()
    stages: dict[str, dict] = {}
    res: dict = {"board": name, "pcb": str(pcb), "stages": stages}

    def py(script, *a):
        return [venv_py, str(scripts / script), "--pcb", str(pcb), *a]

    # each report is set only once its stage ran, so a later stage failing
    # (a route timeout) keeps the seed and anneal results already recorded
    seed: dict = {}
    anneal: dict = {}
    route: dict = {}
    try:
        stages["metrics_orig"], _ = _stage(
            runner, "metrics_orig",
            py("place_metrics.py", "--out", str(bdir / "metrics.orig.json")),
            bdir, env)
        stages["strip"], out = _stage(
            runner, "strip", [bundled_py, "-c", STRIP_PY, str(pcb)], bdir, env)
        m = re.search(r"stripped=(\d+)", out)
        left = len(TRACK_ITEM.findall(pcb.read_text(encoding="utf-8")))
        if not m or left:
            raise CheckError(f"copper strip failed on {name} ({left} track "
                             f"items left): {out.strip()[-200:]}")
        res["stripped"] = int(m[1])
        stages["seed"], out = _stage(
            runner, "seed",
            py("place_seed.py", "--ops-out", str(bdir / "seed.ops.json"),
               "--apply", "--out-report", str(bdir / "seed.json")), bdir, env)
        seed = _report(bdir / "seed.json", out)
        stages["anneal"], out = _stage(
            runner, "anneal",
            py("place_anneal.py", "--out-dir", str(bdir / "anneal"),
               "--apply-best", "--out-report", str(bdir / "anneal.json")),
            bdir, env)
        anneal = _report(bdir / "anneal.json", out)
        stages["metrics_placed"], _ = _stage(
            runner, "metrics_placed",
            py("place_metrics.py", "--out", str(bdir / "metrics.placed.json")),
            bdir, env)
        for tries in range(1, ROUTE_TRIES + 1):
            stages["route"], out = _stage(
                runner, "route",
                py("route_auto.py", "--work-dir", str(bdir / "route"),
                   "--out-report", str(bdir / "route.json")), bdir, env)
            if "wxEntryStart" not in out:
                break
        stages["route"]["tries"] = tries
        route = _report(bdir / "route.json", out)
    except (CheckError, subprocess.TimeoutExpired) as e:
        res["error"] = str(e)
    load = sampler.stop()
    total_s = round(time.monotonic() - t0, 1)

    facts = route.get("facts") or {}
    sev: dict[str, int] = {}
    errors_by_check: dict[str, int] = {}
    for v in route.get("violations") or []:
        sev[v.get("severity")] = sev.get(v.get("severity"), 0) + 1
        if v.get("severity") == "error":
            k = v.get("check") or "?"
            errors_by_check[k] = errors_by_check.get(k, 0) + 1
    orig = _report(bdir / "metrics.orig.json", "")
    placed = _report(bdir / "metrics.placed.json", "")
    res.update({
        "seed": {"status": seed.get("status"),
                 "violations": (seed.get("counts") or {}).get("total"),
                 "error": seed.get("error")},
        "anneal": {"status": anneal.get("status"),
                   "error": anneal.get("error"),
                   "hpwl_best_mm": anneal.get("hpwl_best_mm")},
        "designer": _placement(orig), "placed": _placement(placed),
        "route": {"status": route.get("status"), "error": route.get("error"),
                  "completion": facts.get("completion"),
                  "fr_completion": facts.get("fr_completion"),
                  "routable_nets": facts.get("routable_nets"),
                  "unrouted_nets": facts.get("unrouted_nets"),
                  "best_rung": facts.get("best_rung")},
        "drc": {"errors": sev.get("error", 0),
                "warnings": sev.get("warning", 0),
                "errors_by_check": errors_by_check},
        "total_s": total_s, "load": load,
        # brief: "runtime ... valid only if load stayed < 12"
        "runtime_valid": load["max"] < LOAD_LIMIT,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return _write_result(work, name, res)


def _write_result(work: Path, name: str, res: dict) -> dict:
    tmp = result_path(work, name).with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    tmp.replace(result_path(work, name))
    return res


def start_xvfb() -> tuple[subprocess.Popen, str]:
    """Our own Xvfb: a shared long-lived display has aborted SWIG workers."""
    if not shutil.which("Xvfb"):
        raise CheckError("Xvfb not on PATH (the SWIG and Freerouting steps "
                         "need a display)")
    # -displayfd: Xvfb takes a free display itself and writes its number once
    # it takes connections. Picking the first one with no lock file raced
    # other starts: the loser's server died and its workers were sent to the
    # winner's display (#1037).
    # -noreset: the workers come and go, and a server that resets when the
    # last one leaves fails the next one's connect during the reset (#898)
    r, w = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-nolisten", "tcp",
                             "-noreset"], pass_fds=(w,),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    with os.fdopen(r) as f:
        n = f.readline().strip()
    if not n.isdigit():
        proc.kill()
        proc.wait()
        raise CheckError("Xvfb did not come up")
    return proc, f":{n}"


def run_corpus(root: Path, work: Path, *, boards=None, rerun=(), nice=10,
               xvfb=True, run_one=run_board, **kw) -> dict:
    """Run every corpus board not already done, one at a time."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    lock = open(work / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise CheckError(f"another corpus run holds {work / '.lock'}") from None
    all_boards = corpus_boards(root)
    known = {b.name for b in all_boards}
    for want in list(boards or []) + list(rerun):
        if want not in known:
            raise CheckError(f"no board '{want}' with a kicad/*.kicad_pcb "
                             f"under {root}")
    todo = [b for b in all_boards if not boards or b.name in boards]
    if nice:
        os.nice(nice)
    env = dict(os.environ)
    env.setdefault("JAVA_TOOL_OPTIONS", "-Djava.awt.headless=true")
    xproc = None
    if xvfb:
        xproc, env["DISPLAY"] = start_xvfb()
    ran, skipped = [], []
    try:
        for b in todo:
            if result_path(work, b.name).is_file() and b.name not in rerun:
                skipped.append(b.name)
                continue
            print(f"corpus: {b.name}", file=sys.stderr, flush=True)
            r = run_one(b, work, env=env, **kw)
            ran.append({k: r.get(k) for k in
                        ("board", "error", "route", "drc", "total_s",
                         "runtime_valid")})
    finally:
        if xproc:
            xproc.terminate()
        lock.close()
    return {"work_dir": str(work), "ran": ran, "skipped": skipped,
            "results": [str(result_path(work, b.name)) for b in todo
                        if result_path(work, b.name).is_file()]}
