"""route_close - close a board's open connections with route_finish, in chunks.

route_finish routes shortest first and cuts at --max-conns, so on a stuck board
the same failing connections use up every run and the longer ones are never
tried. A long run that is killed also loses everything it found. route_close
runs route_finish in small chunks and keeps each good one:

- Every open connection gets a try each round. One that fails is set aside
  until the next round, so it doesn't use up the next chunk (route_finish
  --skip-conns). It is the connection that is set aside, not its net: a net
  with 40 open connections still gets the other 39 tried.
  One route_finish reports walled in by fixed copper (#682) stays set aside
  for every round, and the report lists it under "walled".
- Each chunk runs on its own copy of the board. The board is replaced only by
  a chunk that reports, kept copper and passed the protected-copper check, so
  a kill at any moment leaves the board as the last kept chunk left it.
- The chunk stops searching in time to report (--plan-seconds, from the DRC
  time it has measured). If it dies anyway, the connection it was searching is
  set aside and the chunk size halves. A chunk whose routes are rejected as a
  group halves too, until the one route at fault is found and set aside (with
  rip-up, a route and what it ripped can't be split: all are set aside). Clean
  chunks double the size back up to --chunk-conns.
- State is on disk after every step: run the same command again to resume.
  A board that changed since (someone edited it) is measured again, not trusted.
- Protected copper: constraints.json diff pairs by default, --protect-nets, and
  --protect-board for a whole board's copper (`start` = this board as it was
  when the run began). A chunk that loses any of it, or adds copper to a
  protected net, is undone. Open connections on protected nets aren't routed
  (the maze would route a pair single-ended, #613) unless --allow-nets names
  them. Their copper stays protected either way.
- A round that closes nothing tries group routes first (#938): each
  connection that failed pulls in the connections open at the start whose
  search windows (--margin round its two ends) overlap its own; those nets
  are put back as the run found them and routed at once by route_edit's
  route_group (PathFinder), which shares a channel that one-at-a-time
  routing fills in the wrong order. Kept only with fewer open, no error type
  grown and protected copper whole; then another round. facts.grouped counts
  the groups kept.
- It stops when a full round closes nothing, and says why: the facts name the
  connections and nets left, how often each failed, and what to try next.

The runner is the command put in front of each chunk: --runner, else
$HWDE_ROUTE_RUNNER, else `heavy --quick` where `heavy` is on the PATH, else
none. `--runner none` runs chunks directly, which is also the cheap way to run
the whole thing inside one long job: `heavy timeout 7200 hwde route_close
--runner none ...` holds one normal slot and leaves the quick lane alone.

Contract:
  route_close.py --pcb B.kicad_pcb [route_finish's routing options]
      [--budget 3600] [--chunk-timeout 285] [--chunk-conns 12]
      [--runner "CMD ARGS"|none] [--state-dir DIR] [--fresh]
      [--rip-board never|stalled|always]
      [--protect-nets A,B] [--protect-board start|PCB] [--allow-nets A,B]
      [--constraints c.json] [--no-protect-pairs] [--out-report r.json]
  exit 0 = nothing left unconnected and DRC clean; 1 = some remain or other
  violations (board updated with what was kept); 2 = error.
  facts.stop_reason: closed | no_progress | budget | chunks_dying;
  facts.next says what to do about it.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
from checklib import CheckError  # noqa: E402

REPO = SCRIPTS.parent
OVER_LIMIT = {"over --max-conns", "over the deadline", "cut by the deadline"}
NOT_ROUTABLE = {"geometry already joins both items",
                "item not on the net's copper"}
DEAD_LIMIT = 4        # chunks in a row that die with nothing to blame
LOW_TIME_S = 60       # don't start a chunk with less of the budget left
STATE_VERSION = 1
GROUP_MAX_JOBS = 8    # connections one group route takes at most (#938)
GROUP_GRIDS = (0.05, 0.025)  # mm: a group that finds no route tries finer


def default_runner() -> list[str]:
    env_val = os.environ.get("HWDE_ROUTE_RUNNER")
    if env_val is not None:
        return parse_runner(env_val)
    return ["heavy", "--quick"] if shutil.which("heavy") else []


def parse_runner(text: str) -> list[str]:
    return [] if text.strip() in ("", "none") else shlex.split(text)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def key_of(conn: dict) -> list:
    """The [net, a, b] route_finish --skip-conns takes."""
    return [conn["net"], conn["a"], conn["b"]]


def key_str(key: list) -> str:
    return json.dumps([key[0], *sorted(key[1:])])


def group_end(item: dict) -> str | None:
    """A DRC unconnected item as a route_edit END: a pad by name, a track
    end or via by place (on the track's layer). None for anything else."""
    import re

    msg, pos = item.get("msg", ""), item.get("pos")
    m = re.match(r"Pad (\S+) \[.*\] of (\S+) on ", msg)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    if not pos:
        return None
    xy = f"{pos[0]},{pos[1]}"
    m = re.match(r"Track \[.*\] on (\S+?),", msg)
    if m:
        return f"{xy}@{m.group(1)}"
    return xy if msg.startswith("Via ") else None


def _overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _gap(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


# ------------------------------------------------------------------ copper

def copper_keys(pcb: Path) -> dict[str, list[str]]:
    """net -> every track and via on it, by geometry: an item is the same item
    if it's the same copper in the same place, whatever its uuid."""
    import geom

    bg = geom.BoardGeom.from_file(pcb)
    out: dict[str, list[str]] = {}
    for t in bg.tracks_of():
        pts = [(round(x, 4), round(y, 4)) for x, y in t.shape.coords]
        out.setdefault(t.net, []).append(
            f"t {t.layer} {round(t.width, 4)} {min(pts, pts[::-1])}")
    for v in bg.vias_of():
        out.setdefault(v.net, []).append(
            f"v {round(v.at[0], 4)} {round(v.at[1], 4)} {round(v.diameter, 4)}"
            f" {round(v.drill, 4)} {v.layers}")
    return out


def diff_pair_nets(constraints: Path | None) -> set[str]:
    if constraints is None:
        return set()
    data = checklib.load_json(constraints, "constraints")
    return {p[s] for p in data.get("diff_pairs") or []
            for s in ("p", "n") if p.get(s)}


def guard(pcb: Path, base: dict[str, list[str]], protected: set[str],
          allowed: set[str]) -> dict:
    """What this board did to the protected copper: lost and added items."""
    if not base and not protected:
        return {"lost": 0, "added_nets": []}
    now = copper_keys(pcb)
    lost = sum(len(set(items) - set(now.get(net, [])))
               for net, items in base.items())
    added = sorted(net for net in protected - allowed
                   if set(now.get(net, [])) - set(base.get(net, [])))
    return {"lost": lost, "added_nets": added}


# ------------------------------------------------------------------ reports

def open_by_net(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in report["violations"]:
        if v.get("source") == "unconnected" and v.get("net"):
            out[v["net"]] = out.get(v["net"], 0) + 1
    return out


def left_of(report: dict) -> int:
    return sum(1 for v in report["violations"]
               if v.get("source") == "unconnected")


def read_chunk(facts: dict) -> dict:
    """What a chunk's passes tried. Rejected means its pass was undone."""
    res = {"routed": [], "failed": [], "rejected": [], "unroutable": [],
           "cut": [], "over_limit": False, "last_applied": False}
    passes = [p for p in facts.get("passes", []) if "connections" in p]
    undone = bool(facts.get("provisional_undone"))
    # a rip-up run that didn't leave the board better undoes every pass
    all_undone = not facts.get("kept")
    for i, p in enumerate(passes):
        last = i == len(passes) - 1
        for c in p["connections"]:
            st, why = c.get("status"), c.get("reason", "")
            if st in ("failed", "rolled_back"):
                res["failed"].append(c)
            elif st == "routed":
                bad = all_undone or p.get("kept") is False or (
                    undone and p.get("kept") == "provisional")
                res["rejected" if bad else "routed"].append(c)
            elif st == "skipped":
                if last and why in NOT_ROUTABLE:
                    res["unroutable"].append(c)
                if last and why in OVER_LIMIT:
                    res["over_limit"] = True
                if why == "cut by the deadline":
                    res["cut"].append(c)
        if last:
            res["last_applied"] = "unconnected_after" in p
    return res


def chunk_argv(args, pcb: Path, report: Path, work: Path, *, nets, skip_file,
               conns, rip, protect, plan_s, start_report, trace,
               measure=False) -> list[str]:
    import route_finish as rf

    cmd = ["sh", str(REPO / "bin" / "hwde"), "route_finish", "--pcb", str(pcb),
           "--work-dir", str(work), "--out-report", str(report)]
    for name in rf.ROUTING_FLAGS:
        val = getattr(args, name)
        if val is not None and name != "nets":
            cmd += ["--" + name.replace("_", "-"), str(val)]
    if measure:
        return cmd + ["--measure-only"]
    cmd += ["--nets", ",".join(nets), "--max-conns", str(conns),
            "--skip-conns", str(skip_file), "--trace", str(trace),
            "--plan-seconds", str(int(plan_s))]
    if not rip:
        cmd.append("--no-rip-board")
    if protect:
        cmd += ["--protect-nets", ",".join(sorted(protect))]
    if start_report:
        cmd += ["--start-report", str(start_report)]
    return cmd


class Closer:
    def __init__(self, args):
        self.args = args
        self.pcb = Path(args.pcb).resolve()
        if not self.pcb.is_file():
            raise CheckError(f"board not found: {self.pcb}")
        self.dir = (Path(args.state_dir).resolve() if args.state_dir
                    else self.pcb.parent / "route_close")
        self.runner = (default_runner() if args.runner is None
                       else parse_runner(args.runner))
        self.started = time.monotonic()
        self.proc = None
        self.st: dict = {}
        self.base: dict[str, list[str]] = {}
        self.protected: set[str] = set()
        self.allowed = set(filter(None, (args.allow_nets or "").split(",")))
        self.scope = (set(args.nets.split(",")) if args.nets else None)
        self.report: dict = {}

    # ---------------------------------------------------- state on disk

    def routing_opts(self) -> dict:
        import route_finish as rf

        return {k: getattr(self.args, k) for k in rf.ROUTING_FLAGS}

    def save(self) -> None:
        write_json(self.dir / "state.json", self.st)

    def log(self, **entry) -> None:
        with (self.dir / "chunks.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": int(time.time()), **entry}) + "\n")

    def say(self, text: str) -> None:
        print(f"route_close: {text}", file=sys.stderr, flush=True)

    def setup(self) -> None:
        args = self.args
        if args.fresh and (self.dir / "state.json").exists():
            (self.dir / "state.json").unlink()
            (self.dir / "report.json").unlink(missing_ok=True)
            (self.dir / "baseline.json").unlink(missing_ok=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = (self.dir / "lock").open("w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CheckError(f"another route_close holds {self.dir}") from exc
        shutil.rmtree(self.dir / "chunk", ignore_errors=True)
        path = self.dir / "state.json"
        if path.exists():
            self.st = checklib.load_json(path, "state")
            if self.st.get("version") != STATE_VERSION:
                raise CheckError(f"{path} is another version: use --fresh")
            if self.st["opts"] != self.routing_opts():
                self.say("routing options changed: starting a new round")
                self.st["opts"] = self.routing_opts()
                self.new_round()
            self.say(f"resuming from {path}: round {self.st['round']},"
                     f" chunk {self.st['chunk']}")
        else:
            self.st = {
                "version": STATE_VERSION, "opts": self.routing_opts(),
                "board_sha": None, "round": 0, "round_start": None,
                "skip": [], "conns": args.chunk_conns, "chunk": 0,
                "drc_s": 0.0, "fails": {}, "dead": 0, "start_left": None,
                "kept_chunks": 0, "routed": 0, "rejected_chunks": 0,
                "ripping": args.rip_board == "always", "last_failed": {},
                "unroutable": 0}
            shutil.copy2(self.pcb, self.dir / "start.kicad_pcb")
        self.setup_protection()
        rep = self.dir / "report.json"
        if rep.exists():
            self.report = checklib.load_json(rep, "last report")

    def setup_protection(self) -> None:
        args = self.args
        nets = set(filter(None, (args.protect_nets or "").split(",")))
        if not args.no_protect_pairs:
            cons = (Path(args.constraints) if args.constraints
                    else self.pcb.parent / "constraints.json")
            if args.constraints and not cons.is_file():
                raise CheckError(f"constraints not found: {cons}")
            nets |= diff_pair_nets(cons if cons.is_file() else None)
        self.protected = nets
        path = self.dir / "baseline.json"
        if path.exists():
            self.base = checklib.load_json(path, "baseline")
            return
        base: dict[str, list[str]] = {}
        if nets:
            now = copper_keys(self.dir / "start.kicad_pcb")
            base = {n: now[n] for n in nets if n in now}
        if args.protect_board:
            other = (self.dir / "start.kicad_pcb"
                     if args.protect_board == "start"
                     else Path(args.protect_board))
            if not other.is_file():
                raise CheckError(f"protect board not found: {other}")
            for net, items in copper_keys(other).items():
                base[net] = sorted(set(base.get(net, [])) | set(items))
        self.base = base
        write_json(path, base)

    def new_round(self) -> None:
        # a connection walled in by fixed copper (#682) stays set aside: a
        # new round's routes can't free it
        self.st.update(round=self.st["round"] + 1, last_failed={},
                       skip=[w["key"] for w in self.st.get("walled",
                                                           {}).values()],
                       round_start=left_of(self.report) if self.report else None,
                       unroutable=0)

    # ---------------------------------------------------- running a chunk

    def remaining(self) -> float:
        return self.args.budget - (time.monotonic() - self.started)

    def run_chunk(self, make_argv, timeout: int) -> dict:
        """One route_finish on a copy of the board. The chunk number is saved
        first, so a stray chunk from a killed run can't write into a later
        one's directory."""
        self.st["chunk"] += 1
        self.save()
        cdir = self.dir / "chunk" / f"{self.st['chunk']:05d}"
        stage = cdir / "b"
        stage.mkdir(parents=True)
        for src in self.pcb.parent.glob(self.pcb.stem + ".*"):
            if src.is_file() and not src.name.endswith(".lck"):
                shutil.copy2(src, stage / src.name)
        sha_before = sha(self.pcb)
        report, trace = cdir / "r.json", cdir / "trace.jsonl"
        skip_file = cdir / "skip.json"
        write_json(skip_file, self.st["skip"])
        argv = make_argv(stage / self.pcb.name, report, cdir / "w", skip_file,
                         trace)
        inner = (["timeout", "--kill-after=10", str(timeout)]
                 if shutil.which("timeout") else [])
        cmd = self.runner + inner + argv
        t0 = time.monotonic()
        with (cdir / "out.log").open("wb") as out:
            self.proc = subprocess.Popen(cmd, stdout=out,
                                         stderr=subprocess.STDOUT,
                                         start_new_session=True)
            try:
                # the inner timeout ends the job; this is for a runner that
                # never starts it, or a machine with no `timeout`
                rc = self.proc.wait(timeout=max(self.remaining(), 0)
                                    + timeout + 60)
            except subprocess.TimeoutExpired:
                self.kill_chunk()
                rc = self.proc.wait()
            self.proc = None
        res = {"rc": rc, "s": int(time.monotonic() - t0), "dir": cdir,
               "board": stage / self.pcb.name, "sha_before": sha_before,
               "report": None, "trace": trace}
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            if isinstance(data.get("facts"), dict):
                res["report"] = data
        except (OSError, ValueError):
            pass
        if res["report"] is None:
            tail = (cdir / "out.log").read_bytes()[-400:]
            res["why"] = tail.decode("utf-8", "replace").strip()
        return res

    def kill_chunk(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def adopt(self, res: dict) -> None:
        """Take a chunk's board and report as the real ones. A crash between
        the two leaves a sha that doesn't match, and the next start measures
        the board again."""
        if sha(self.pcb) != res["sha_before"]:
            raise CheckError(f"{self.pcb} changed while a chunk ran (someone"
                             " else is editing it): stopped, nothing lost")
        tmp = self.pcb.with_name(self.pcb.name + ".route_close.tmp")
        shutil.copy2(res["board"], tmp)
        os.replace(tmp, self.pcb)
        self.take_report(res["report"])
        self.st["board_sha"] = sha(self.pcb)

    def take_report(self, report: dict) -> None:
        self.report = report
        write_json(self.dir / "report.json", report)

    def plan_seconds(self, timeout: int) -> float:
        """Leave room for the DRC runs after the plan: it may rerun after a
        rollback, and the chunk's own stage and report take a few seconds."""
        reserve = 2.5 * self.st["drc_s"] + 15
        return max(timeout - reserve, min(30.0, timeout / 2))

    def measure(self) -> bool:
        """Refill and DRC the board as it is: the first step, or after someone
        changed it. False if that can't be done."""
        self.say("measuring the board (refill and DRC)")
        timeout = int(min(self.args.chunk_timeout, max(self.remaining(), 30)))
        res = self.run_chunk(
            lambda pcb, rep, work, skip, trace: chunk_argv(
                self.args, pcb, rep, work, nets=(), skip_file=skip, conns=0,
                rip=False, protect=(), plan_s=0, start_report=None,
                trace=trace, measure=True),
            timeout)
        if res["report"] is None:
            self.st["died_why"] = res.get("why", "")
            self.log(chunk=self.st["chunk"], measure=True, rc=res["rc"],
                     died=True, s=res["s"], why=res.get("why", "")[-200:])
            shutil.rmtree(res["dir"], ignore_errors=True)
            return False
        self.adopt(res)
        self.st["drc_s"] = res["report"]["facts"].get("drc_s", 0.0)
        if self.st["start_left"] is None:
            self.st["start_left"] = left_of(self.report)
            # the open connections a group step routes again (#938)
            write_json(self.dir / "start_report.json", self.report)
        if self.st["round_start"] is None:
            self.st["round_start"] = left_of(self.report)
        self.log(chunk=self.st["chunk"], measure=True, s=res["s"],
                 left=left_of(self.report), drc_s=self.st["drc_s"])
        self.save()
        shutil.rmtree(res["dir"], ignore_errors=True)
        return True

    # ---------------------------------------------------- the rounds

    def todo_nets(self) -> list[str]:
        out = []
        for net in open_by_net(self.report):
            if self.scope is not None and net not in self.scope:
                continue
            if net in self.protected and net not in self.allowed:
                continue
            out.append(net)
        return out

    def set_aside(self, conn: dict, why: str) -> None:
        key = key_of(conn)
        if key not in self.st["skip"]:
            self.st["skip"].append(key)
        net = self.st["fails"].setdefault(conn["net"],
                                          {"fails": 0, "reasons": {}})
        net["fails"] += 1
        net["reasons"][why] = net["reasons"].get(why, 0) + 1
        self.st["last_failed"][key_str(key)] = {"net": conn["net"],
                                                "a": conn["a"], "b": conn["b"],
                                                "reason": why}
        if conn.get("walled"):
            self.st.setdefault("walled", {})[key_str(key)] = {
                "key": key, "net": conn["net"], "a": conn["a"],
                "b": conn["b"], "reason": why}

    def shrink(self) -> None:
        self.st["conns"] = max(1, self.st["conns"] // 2)

    def grow(self) -> None:
        self.st["conns"] = min(self.args.chunk_conns, self.st["conns"] * 2)

    def last_try(self, trace: Path) -> dict | None:
        try:
            lines = trace.read_text(encoding="utf-8").splitlines()
            net, a, b = json.loads(lines[-1])
        except (OSError, ValueError, IndexError):
            return None
        return {"net": net, "a": a, "b": b}

    def died(self, res: dict) -> None:
        conn = self.last_try(res["trace"])
        self.shrink()
        if conn is not None and key_of(conn) not in self.st["skip"]:
            self.set_aside(conn, f"chunk died in this search (rc {res['rc']})")
            self.st["dead"] = 0
        else:
            self.st["dead"] += 1
        self.st["died_why"] = res.get("why", "")
        self.log(chunk=self.st["chunk"], died=True, rc=res["rc"], s=res["s"],
                 blamed=conn, conns=self.st["conns"],
                 why=res.get("why", "")[-200:])
        self.say(f"chunk {self.st['chunk']} died (rc {res['rc']}, {res['s']} s)"
                 f", blamed {conn and conn['net']}, size {self.st['conns']}")

    def one_chunk(self, nets: list[str]) -> bool:
        """Run a chunk, keep or undo it. True when the round has more to try."""
        timeout = int(min(self.args.chunk_timeout, self.remaining()))
        plan_s = self.plan_seconds(timeout)
        rip = self.st["ripping"]
        start_rep = self.dir / "report.json"
        conns = self.st["conns"]
        res = self.run_chunk(
            lambda pcb, rep, work, skip, trace: chunk_argv(
                self.args, pcb, rep, work, nets=nets, skip_file=skip,
                conns=conns, rip=rip, protect=self.protected, plan_s=plan_s,
                start_report=start_rep, trace=trace),
            timeout)
        try:
            if res["report"] is None:
                self.died(res)
                self.save()
                return True
            return self.judge(res, conns, nets)
        finally:
            shutil.rmtree(res["dir"], ignore_errors=True)

    def judge(self, res: dict, conns: int, nets: list[str]) -> bool:
        facts = res["report"]["facts"]
        seen = read_chunk(facts)
        aside_before = len(self.st["skip"])
        self.st["drc_s"] = facts.get("drc_s") or self.st["drc_s"]
        for c in seen["failed"]:
            self.set_aside(c, c.get("reason", c.get("status", "failed")))
        self.st["unroutable"] = len(seen["unroutable"])
        err = next((p["error"] for p in facts.get("passes", [])
                    if "error" in p), None)
        kept = bool(facts.get("kept"))
        hurt = (guard(res["board"], self.base, self.protected, self.allowed)
                if kept else {"lost": 0, "added_nets": []})
        undo = bool(hurt["lost"] or hurt["added_nets"])
        suspects = list(seen["rejected"])
        if undo:
            suspects += seen["routed"]
            self.st["rejected_chunks"] += 1
            kept = False
        elif kept:
            self.adopt(res)
            self.st["kept_chunks"] += 1
            self.st["routed"] += len(seen["routed"])
        retry = False
        if len(suspects) == 1:
            self.set_aside(suspects[0], "its route was undone: "
                           + ("it hurt protected copper" if undo else
                              "the pass didn't lower the open count, or an"
                              " error type grew"))
        elif suspects and (conns == 1 or self.st["ripping"]):
            # can't be split further: a size-1 chunk rejects several routes
            # when its passes moved on, and with rip-up a route and the
            # neighbours it ripped stand or fall together, so a smaller
            # chunk would only repeat them
            for c in suspects:
                self.set_aside(c, "its route was undone: "
                               + ("it hurt protected copper" if undo else
                                  "the pass didn't lower the open count, or"
                                  " an error type grew"))
            retry = True
        elif suspects:
            # one of them is at fault, not all: halve until it's alone
            self.st["conns"] = min(self.st["conns"], max(1, len(suspects) // 2))
            retry = True
        if err is not None and not seen["routed"] and not suspects:
            # the pass blew up (a post-apply check): blame what it searched
            conn = self.last_try(res["trace"])
            if conn is not None and key_of(conn) not in self.st["skip"]:
                self.set_aside(conn, f"route_finish error: {err[:120]}")
            self.shrink()
            retry = True
        for c in seen["cut"]:
            # cut by the deadline at the head of a chunk that found nothing:
            # this search is longer than a chunk, so it can't ever finish
            if not (seen["routed"] or seen["failed"] or suspects):
                self.set_aside(c, "its search outlasted the chunk's time")
                break
        # a chunk that changed nothing would run again just the same
        if kept or len(self.st["skip"]) > aside_before \
                or self.st["conns"] < conns:
            self.st["dead"] = 0
        else:
            self.st["dead"] += 1
            self.st["died_why"] = (
                "a chunk ran out of time before its first search: raise"
                " --chunk-timeout" if seen["over_limit"] and not (
                    seen["routed"] or seen["failed"] or seen["rejected"]
                    or seen["cut"])
                else "chunks kept nothing and set nothing aside")
        if not suspects and not err and conns >= self.st["conns"]:
            self.grow()
        self.log(chunk=self.st["chunk"], round=self.st["round"], s=res["s"],
                 conns=conns, nets=len(nets), rip=self.st["ripping"], kept=kept,
                 routed=len(seen["routed"]), failed=len(seen["failed"]),
                 rejected=len(seen["rejected"]), lost=hurt["lost"],
                 added=hurt["added_nets"], left=left_of(self.report),
                 drc_s=self.st["drc_s"], next_conns=self.st["conns"], err=err)
        self.say(f"chunk {self.st['chunk']} round {self.st['round']}"
                 f" ({res['s']} s, size {conns}): routed {len(seen['routed'])}"
                 f" failed {len(seen['failed'])} kept {kept}"
                 f" left {left_of(self.report)}")
        self.save()
        return retry or seen["over_limit"] or seen["last_applied"] \
            or bool(seen["cut"])

    def run_round(self) -> str | None:
        """A stop reason if the budget or the chunks ran out mid-round."""
        while True:
            if self.remaining() < LOW_TIME_S:
                return "budget"
            if self.st["dead"] >= DEAD_LIMIT:
                return "chunks_dying"
            nets = self.todo_nets()
            if not nets or not self.one_chunk(nets):
                return None

    def progressed(self, start: int, left: int) -> bool:
        import route_finish as rf

        if left < start:
            return True
        return (start >= rf.DRC_UNCONNECTED_CAP
                and left >= rf.DRC_UNCONNECTED_CAP
                and self.st["routed"] > self.st.get("round_routed", 0))

    # ---------------------------------------------------- group routes

    def group_jobs(self) -> list[list[dict]]:
        """Clusters of open connections to route together (#938), largest
        first. Each stuck connection (one that failed this round) pulls in
        every connection that was open when the run began whose search window
        overlaps its own, and with a net all of that net's: the group step
        puts those nets back as the run found them and routes them all at
        once with route_edit's route_group (PathFinder), which can share a
        channel that one-at-a-time routing fills in the wrong order. Nets
        never grouped: protected ones, power, pour and rule-width nets (as
        reroute), out of --nets. A cluster needs two or more connections and
        at most GROUP_MAX_JOBS."""
        import geom
        import route_finish as rf
        from check_testpoints import is_power

        path = self.dir / "start_report.json"
        if not path.is_file() or not self.st["last_failed"]:
            return []
        start = checklib.load_json(path, "start report")
        bg = geom.BoardGeom.from_file(self.pcb)
        # sized by rule, so never ripped (mazeroute.Router.keep_nets)
        rules = rf.board_rules(self.pcb, bg)
        never = (rf.pour_nets(bg) | set(rules.net_width)
                 | rules.sized_nets | {""})
        margin = self.args.margin
        conns = []
        for v in start["violations"]:
            net = v.get("net")
            items = v.get("items") or []
            if v.get("source") != "unconnected" or not net or len(items) < 2:
                continue
            ends = [group_end(it) for it in items[:2]]
            ok = (None not in ends and net not in never and not is_power(net)
                  and (self.scope is None or net in self.scope)
                  and (net not in self.protected or net in self.allowed))
            xs = [it["pos"][0] for it in items[:2]]
            ys = [it["pos"][1] for it in items[:2]]
            conns.append({"net": net, "from": ends[0], "to": ends[1],
                          "ok": ok, "box": (min(xs) - margin, min(ys) - margin,
                                            max(xs) + margin,
                                            max(ys) + margin)})
        by_net: dict[str, list[dict]] = {}
        for c in conns:
            by_net.setdefault(c["net"], []).append(c)
        out, seen = [], set()
        for f in self.st["last_failed"].values():
            if f["net"] not in by_net or not all(
                    c["ok"] for c in by_net[f["net"]]):
                continue
            xs, ys = (f["a"][0], f["b"][0]), (f["a"][1], f["b"][1])
            box = (min(xs) - margin, min(ys) - margin, max(xs) + margin,
                   max(ys) + margin)
            near = sorted({c["net"] for c in conns
                           if c["ok"] and _overlap(box, c["box"])}
                          - {f["net"]},
                          key=lambda n: min(_gap(box, c["box"])
                                            for c in by_net[n]))
            jobs = list(by_net[f["net"]])
            for net in near:
                if all(c["ok"] for c in by_net[net]) and \
                        len(jobs) + len(by_net[net]) <= GROUP_MAX_JOBS:
                    jobs += by_net[net]
            key = tuple(sorted({j["net"] for j in jobs}))
            if len(jobs) >= 2 and len(jobs) <= GROUP_MAX_JOBS \
                    and key not in seen:
                seen.add(key)
                out.append([{k: j[k] for k in ("net", "from", "to")}
                            for j in jobs])
        return sorted(out, key=len, reverse=True)

    def try_groups(self) -> bool:
        """Route each cluster from group_jobs together; keep a cluster's board
        only if it has fewer open connections, no error type grew, and the
        protected copper held. True when one was kept."""
        import route_finish as rf

        tried = self.st.setdefault("group_tried", [])
        for jobs in self.group_jobs():
            nets = sorted({j["net"] for j in jobs})
            key = json.dumps([self.st["board_sha"], nets])
            if key in tried:
                continue
            tried.append(key)
            for grid in GROUP_GRIDS:
                if self.remaining() < LOW_TIME_S:
                    return False
                res = self.group_chunk(nets, jobs, grid)
                try:
                    rep = res["report"]
                    if rep is None:
                        self.log(chunk=self.st["chunk"], group=nets,
                                 grid=grid, kept=False, s=res["s"],
                                 why=res.get("why", "")[-200:])
                        self.say(f"group {','.join(nets)} at grid {grid}:"
                                 " no route")
                        continue
                    before, after = left_of(self.report), left_of(rep)
                    errs, errs0 = rf.err_counts(rep), rf.err_counts(self.report)
                    grew = sorted(k for k, n in errs.items()
                                  if n > errs0.get(k, 0))
                    hurt = guard(res["board"], self.base, self.protected,
                                 self.allowed)
                    kept = (after < before and not grew and not hurt["lost"]
                            and not hurt["added_nets"])
                    self.log(chunk=self.st["chunk"], group=nets, grid=grid,
                             kept=kept, s=res["s"], left=after, grew=grew,
                             lost=hurt["lost"])
                    self.say(f"group {','.join(nets)} at grid {grid}:"
                             f" {before} -> {after} open"
                             + (", kept" if kept else ", undone"
                                + (f" ({', '.join(grew)} grew)" if grew
                                   else "")))
                    if kept:
                        self.adopt(res)
                        self.st["kept_chunks"] += 1
                        self.st["grouped"] = self.st.get("grouped", 0) + 1
                        self.save()
                        return True
                    break   # it routed: a finer grid won't do better
                finally:
                    shutil.rmtree(res["dir"], ignore_errors=True)
            self.save()
        return False

    def group_chunk(self, nets: list[str], jobs: list[dict],
                    grid: float) -> dict:
        """route_edit on a copy of the board: the nets put back as the run
        found them (rip, then copy_from the start board), then one
        route_group; then refill and DRC. No report when route_edit
        refused."""
        start = self.dir / "start.kicad_pcb"
        group = {"op": "route_group", "jobs": jobs, "grid": grid}
        if self.args.layers and self.args.layers != "auto":
            group["layers"] = self.args.layers.split(",")
        ops = {"version": 1, "ops": [
            {"op": "rip", "net": nets},
            {"op": "copy_from", "pcb": str(start), "net": nets}, group]}
        timeout = int(min(self.args.chunk_timeout, self.remaining()))
        hwde = str(REPO / "bin" / "hwde")

        def argv(pcb, rep, work, skip, trace):
            write_json(rep.parent / "ops.json", ops)
            measure = chunk_argv(
                self.args, pcb, rep, work, nets=(), skip_file=skip, conns=0,
                rip=False, protect=(), plan_s=0, start_report=None,
                trace=trace, measure=True)
            # route_edit's failure leaves no report: the measure never runs
            return ["sh", "-c", 'sh "$1" route_edit --pcb "$2" --ops "$3"'
                    ' || exit 3; shift 3; exec "$@"', "group", hwde,
                    str(pcb), str(rep.parent / "ops.json"), *measure]

        return self.run_chunk(argv, timeout)

    def run_all(self) -> str:
        args = self.args
        now_sha = sha(self.pcb)
        if not self.report or self.st["board_sha"] != now_sha:
            if self.report:
                self.say("the board isn't what the last chunk left: measuring")
            if not self.measure():
                raise CheckError("can't measure the board: "
                                 + self.st.get("died_why", "")[-300:])
        if not self.st["round"]:
            self.new_round()
        self.save()
        while True:
            self.st.setdefault("round_routed", self.st["routed"])
            start = self.st["round_start"]
            stop = self.run_round()
            if stop:
                return stop
            left = left_of(self.report)
            nets = self.todo_nets()
            self.log(round_end=self.st["round"], start=start, left=left,
                     aside=len(self.st["skip"]), rip=self.st["ripping"])
            self.say(f"round {self.st['round']} ended: {start} -> {left} open,"
                     f" {len(self.st['skip'])} set aside")
            if not nets:
                return "closed"
            if self.progressed(start, left):
                pass
            elif self.try_groups():
                self.say("a group route closed some: another round")
            elif args.rip_board == "stalled" and not self.st["ripping"]:
                self.say("no progress: allowing board rip-up from here")
                self.st["ripping"] = True
            else:
                return "no_progress"
            self.st["round_routed"] = self.st["routed"]
            self.new_round()
            self.save()

    # ---------------------------------------------------- the answer

    def next_step(self, reason: str, left_nets: list[str], prot: dict) -> str:
        if reason == "closed":
            if not left_nets and not prot:
                return "No open connections in scope. Run the DRC gate next."
            return ("Everything route_close may route is closed. Left: the"
                    " protected nets in facts.protected_open, for the pair"
                    " router; or --allow-nets if a stub is all they need.")
        if reason == "budget":
            return "Out of budget, with progress saved: run the same command" \
                   " again to carry on."
        if reason == "chunks_dying":
            return "Chunks die with nothing to blame (facts.died_why). Fix" \
                   " that first: more time per chunk, a runner with more" \
                   " memory, or a look at the error. Then rerun to resume."
        rip = "" if self.st["ripping"] else (
            " Or allow rip-up with --rip-board stalled.")
        return ("A full round closed nothing: facts.still_failing have no path"
                " at these options. That is a placement or topology finding."
                " A finer --grid, a lower --min-width, or a smaller via may"
                f" still open some.{rip}")

    def payload(self, reason: str) -> dict:
        report = self.report or {"violations": [], "counts": {"total": -1}}
        by_net = open_by_net(report)
        prot = {n: c for n, c in by_net.items()
                if n in self.protected and n not in self.allowed}
        out_of_scope = {n: c for n, c in by_net.items()
                        if self.scope is not None and n not in self.scope}
        left_nets = sorted(self.todo_nets()) if self.report else []
        fails = {n: f for n, f in self.st["fails"].items() if n in by_net}
        left = left_of(report)
        facts = {
            "stop_reason": reason,
            "unconnected_start": self.st["start_left"],
            "unconnected_left": left,
            "errors_after": _err_counts(report) if self.report else {},
            "rounds": self.st["round"], "chunks": self.st["chunk"],
            "kept_chunks": self.st["kept_chunks"],
            "undone_for_protected_copper": self.st["rejected_chunks"],
            "routed": self.st["routed"],
            "grouped": self.st.get("grouped", 0),
            "elapsed_s": int(time.monotonic() - self.started),
            "open_by_net": {n: by_net[n] for n in left_nets},
            "protected_open": prot,
            "out_of_scope_open": out_of_scope,
            "failed_nets": dict(sorted(fails.items(),
                                       key=lambda kv: -kv[1]["fails"])),
            "still_failing": list(self.st["last_failed"].values()),
            # #682: walled in by fixed copper: set aside for every round
            "walled": [{k: v for k, v in w.items() if k != "key"}
                       for w in self.st.get("walled", {}).values()],
            "not_routable_by_maze": self.st["unroutable"],
            "rip_board": self.st["ripping"],
            "state_dir": str(self.dir),
            "start_board": str(self.dir / "start.kicad_pcb"),
            "next": self.next_step(reason, left_nets, prot)}
        if reason == "chunks_dying":
            facts["died_why"] = self.st.get("died_why", "")
        total = report["counts"].get("total", -1)
        return {"script": "route_close", "board": str(self.pcb),
                "status": "pass" if total == 0 else "violations",
                "counts": report["counts"],
                "violations": report["violations"], "facts": facts}


def _err_counts(report: dict) -> dict[str, int]:
    import route_finish as rf

    return rf.err_counts(report)


def run(argv: list[str] | None = None):
    import route_finish as rf

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    rf.add_routing_args(ap)
    ap.add_argument("--budget", type=float, default=3600.0,
                    help="seconds for this run, queue waits included")
    ap.add_argument("--chunk-timeout", type=int, default=285,
                    help="seconds a chunk may take; the quick lane allows 300")
    ap.add_argument("--chunk-conns", type=int, default=12,
                    help="connections per pass of a chunk, at most")
    ap.add_argument("--runner", default=None,
                    help="command before each chunk (`none` = run directly);"
                         " default $HWDE_ROUTE_RUNNER, else `heavy --quick`"
                         " if heavy is on the PATH, else none")
    ap.add_argument("--state-dir", default=None,
                    help="default: route_close/ beside the board")
    ap.add_argument("--fresh", action="store_true",
                    help="forget the saved state and start over from the board"
                         " as it is")
    ap.add_argument("--rip-board", choices=("never", "stalled", "always"),
                    default="never",
                    help="rip other nets' copper: never, only once a round"
                         " makes no progress, or from the start")
    ap.add_argument("--protect-nets", default=None,
                    help="nets whose copper a chunk may not change (comma list)")
    ap.add_argument("--protect-board", default=None,
                    help="`start` or a board file: none of its tracks and vias"
                         " may be lost")
    ap.add_argument("--allow-nets", default=None,
                    help="protected nets that may be routed and gain copper,"
                         " e.g. a pair stub left open (comma list)")
    ap.add_argument("--constraints", default=None,
                    help="default: constraints.json beside the board")
    ap.add_argument("--no-protect-pairs", action="store_true",
                    help="don't protect constraints.json diff pairs")
    ap.add_argument("--out-report", default=None)
    args = ap.parse_args(argv)
    if args.chunk_conns < 1:
        raise CheckError("--chunk-conns must be at least 1")
    closer = Closer(args)

    def on_term(signum, _frame):
        closer.kill_chunk()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)
    closer.setup()
    reason = closer.run_all()
    closer.save()
    payload = closer.payload(reason)
    f = payload["facts"]
    closer.say(f"stopped: {reason}. {f['unconnected_start']} -> "
               f"{f['unconnected_left']} open in {f['chunks']} chunks,"
               f" {f['elapsed_s']} s. {f['next']}")
    return payload, args.out_report


def main(argv: list[str] | None = None) -> int:
    return checklib.cli_wrap("route_close", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
