"""A stand-in for `timeout N sh hwde route_finish ...`, used as route_close's
--runner by test_route_close.py, so the driver's loop runs with no router.

Its board is real s-expressions with the open connections kept in a
(fake_open ...) node, so undoing a chunk undoes its routes. World file (JSON,
$FAKE_WORLD): {conn id: kind}. Connections sort by id, like shortest first.
Kinds:
  ok      routes
  fail    no path
  walled  no path: walled in by fixed copper (route_finish's #682 note)
  die     the chunk is killed searching it (no report)
  poison  routes, but its pass is rejected as a group
  lose    routes, and takes the board's first track with it
  grabs   routes, and adds a track on net PAIR_P
  riponly routes only without --no-rip-board
  ripundone routes in a kept pass, then the whole rip-up run is undone
  overtime is out of time before its search
  chain   is undone along with a second route (a neighbour it ripped)
  cut     its search runs into the deadline
A world key "kill_at" SIGKILLs route_close at that chunk number. Every call is
appended to $FAKE_CALLS.
"""
import json
import os
import re
import signal
import sys
from pathlib import Path


def arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def conn(cid):
    return {"net": cid.split(":")[0], "a": [float(len(cid)), 0.0],
            "b": [float(sum(map(ord, cid))), 1.0]}


def main():
    argv = sys.argv[1:]
    world = json.loads(Path(os.environ["FAKE_WORLD"]).read_text())
    pcb = Path(arg(argv, "--pcb"))
    report = Path(arg(argv, "--out-report"))
    text = pcb.read_text()
    match = re.search(r'\(fake_open([^)]*)\)', text)
    opened = re.findall(r'"([^"]+)"', match[1])
    measure = "--measure-only" in argv
    conns = arg(argv, "--max-conns")
    calls = Path(os.environ["FAKE_CALLS"])
    with calls.open("a") as fh:
        fh.write(json.dumps({"measure": measure, "conns": conns,
                             "nets": arg(argv, "--nets"),
                             "rip": "--no-rip-board" not in argv,
                             "protect": arg(argv, "--protect-nets"),
                             "start": arg(argv, "--start-report")}) + "\n")
    n_calls = len(calls.read_text().splitlines())
    if world.get("kill_at") == n_calls:
        os.kill(os.getppid(), signal.SIGKILL)
    if world.get("broken"):
        print(json.dumps({"status": "error", "error": "kicad-cli not found"}))
        return 2

    def emit(left, facts):
        violations = [{"source": "unconnected", "net": c.split(":")[0],
                       "severity": "error", "items": []} for c in left]
        report.write_text(json.dumps({
            "script": "route_finish", "counts": {"total": len(violations)},
            "violations": violations, "facts": facts}))
        return 1 if violations else 0

    if measure:
        return emit(opened, {"kept": False, "drc_s": 1.0, "passes": []})
    skip = {tuple(map(json.dumps, k)) for k in
            json.loads(Path(arg(argv, "--skip-conns")).read_text())}
    nets = set(arg(argv, "--nets").split(","))
    trace = Path(arg(argv, "--trace"))
    jobs, routed = [], []
    tried = 0
    poison = lose = grabs = undone_after = False
    for cid in sorted(opened):
        kind = world.get(cid, "ok")
        job = conn(cid)
        if job["net"] not in nets:
            continue
        if (json.dumps(job["net"]), json.dumps(job["a"]),
                json.dumps(job["b"])) in skip:
            jobs.append({**job, "status": "skipped", "reason": "set aside"})
            continue
        if tried >= int(conns):
            jobs.append({**job, "status": "skipped",
                         "reason": "over --max-conns"})
            continue
        tried += 1
        with trace.open("a") as fh:
            fh.write(json.dumps([job["net"], job["a"], job["b"]]) + "\n")
        if kind == "die":
            os._exit(137)
        if kind == "overtime":
            jobs.append({**job, "status": "skipped",
                         "reason": "over the deadline"})
            continue
        if kind == "cut":
            jobs.append({**job, "status": "skipped",
                         "reason": "cut by the deadline"})
        elif kind == "walled":
            jobs.append({**job, "status": "failed", "walled": True,
                         "reason": "no path: U1 pad 3 walled in: " + cid})
        elif kind == "fail" or (kind == "riponly"
                                and "--no-rip-board" in argv):
            jobs.append({**job, "status": "failed",
                         "reason": "no path on the grid"})
        else:
            jobs.append({**job, "status": "routed"})
            routed.append(cid)
            poison |= kind == "poison"
            undone_after |= kind == "ripundone"
            lose |= kind == "lose"
            grabs |= kind == "grabs"
            if kind == "chain":
                poison = True
                jobs.append({**job, "net": job["net"] + "_ripped",
                             "status": "routed"})
    entry = {"pass": 1, "pairs": len(jobs), "connections": jobs}
    if routed:
        entry["unconnected_after"] = len(opened) - len(routed)
    if undone_after:
        entry["kept"] = True
        return emit(opened, {"kept": False, "rip_kept": False,
                             "passes": [entry]})
    if poison:
        entry["kept"] = False
        return emit(opened, {"kept": False, "passes": [entry]})
    if not routed:
        return emit(opened, {"kept": False, "passes": [entry]})
    entry["kept"] = True
    left = [c for c in opened if c not in routed]
    text = text.replace(match[0], "(fake_open "
                        + " ".join(f'"{c}"' for c in left) + ")")
    if lose:
        text = re.sub(r'\(segment [^\n]*\n', "", text, count=1)
    if grabs:
        text = text.replace("(fake_open", '(segment (start 1 1) (end 2 2)'
                            ' (width 0.2) (layer "F.Cu") (net "PAIR_P"))\n'
                            '  (fake_open', 1)
    pcb.write_text(text)
    return emit(left, {"kept": True, "drc_s": 1.0, "passes": [entry]})


if __name__ == "__main__":
    sys.exit(main())
