"""routelib - shared plumbing for the S11 routing pipeline (venv side).

Three concerns, used by route_edit.py / route_auto.py / planes_gen.py:

1. run_worker(): drive lib/route_swig.py in KiCad's BUNDLED python. The job
   and result travel via FILES in a caller-owned staging dir - worker stdout
   is noise by design (wx image-handler chatter, C-level "memory leak of type
   'PCB_TRACK *'" spray on bulk removals) and is never parsed.

2. Freerouting invocation: build_fr_cmd() with the S11-verified flag set
   (LEARNINGS [freerouting]): --gui.enabled=false, -de/-do, -mp, and the
   determinism/safety trio -mt 1 -is sequential -da (multithreaded optimizer
   has a known clearance bug AND is nondeterministic; -da is mandatory or FR
   phones home and can stall) + --logging.file.enabled=false (else a
   DEBUG-heavy freerouting.log lands in cwd). run_freerouting() enforces a
   PROCESS-level timeout - a wedged JVM is only cleared by a kill.

3. parse_fr_log(): the authoritative completion parse. FR's own success
   signal lies; unrouted-count priority is (a) session-completed line's
   "final score: S (N unrouted)", (b) the LAST pass line's "(N unrouted)"
   - a pass line WITHOUT the parenthetical means 0 unrouted at that pass -
   (c) None (unknown -> caller treats as failure). Gate on kicad-cli DRC,
   never on FR's numbers.

4. dsn_apply_net_rules(): KiCad's DSN export writes only netclass rules, so
   a per-net .kicad_dru floor (rules_gen's aiee_pwr_width_* track width and
   aiee_hv_* clearance) never reaches Freerouting, which then lays that net
   at the default 0.2 mm (rf-term's /RF: 4 track_width + 3 HV clearance DRC
   errors). Each such net is moved into its own DSN class carrying the
   floor.

5. dsn_merge_wires(): KiCad exports pre-routed copper as many short wires,
   which overflow Freerouting's stack in PolylineTrace.combine; each chain
   of same-net/layer/width/type wires is joined into one path.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import loadlib
from checklib import CheckError

WORKER = Path(__file__).resolve().parent / "route_swig.py"

# Deterministic / safe base flags (order: options first, then -de/-do).
FR_BASE_FLAGS = ["--gui.enabled=false", "-mt", "1", "-is", "sequential",
                 "-da", "--logging.file.enabled=false"]

# Freerouting 2.2.4 hangs when a routed trace lies along a fixed wire
# (BasicBoard.normalize_traces never ends; #477, #657, #665). This folder holds
# that one class with the loop bounded, ahead of the jar on the classpath; its
# README says how it was built. A later jar gets no overlay: 2.5.0 has its own cap.
FR_OVERLAY = Path(__file__).resolve().parent / "freerouting_2_2_4"
FR_OVERLAY_JAR = "freerouting-2.2.4.jar"
FR_MAIN_CLASS = "app.freerouting.Freerouting"

# Freerouting's JVM heap (#897). With no -Xmx Java takes a quarter of the
# machine's RAM, so several routes at once outgrow a shared machine and get
# killed for memory. The cap is a share of the job's own memory budget
# (loadlib.memory_budget_mb: the tightest cgroup limit, else RAM), kept
# between a floor a small board needs and a ceiling no board here has needed.
# HWDE_FR_HEAP_MB sets it outright; 0 leaves Java's default. A JVM that runs
# out exits at once (ExitOnOutOfMemoryError) instead of thrashing in GC, and
# run_freerouting reports it as "oom".
FR_HEAP_SHARE = 0.5     # the rest is the JVM's own overhead and our process
FR_HEAP_MIN_MB = 512
FR_HEAP_MAX_MB = 4096
FR_OOM_EXIT = "-XX:+ExitOnOutOfMemoryError"
_OOM_RE = re.compile(r"java\.lang\.OutOfMemoryError")


def fr_heap_mb(budget_mb: int | None = None) -> int | None:
    """Freerouting's -Xmx in MB, or None for no cap (see FR_HEAP_SHARE)."""
    env = os.environ.get("HWDE_FR_HEAP_MB", "").strip()
    if env:
        try:
            mb = int(env)
        except ValueError:
            raise CheckError(f"HWDE_FR_HEAP_MB={env!r} is not a whole number "
                             "of MB (0 = no cap)") from None
        return mb if mb > 0 else None
    if budget_mb is None:
        budget_mb = loadlib.memory_budget_mb()
    if budget_mb is None:
        return FR_HEAP_MAX_MB
    return int(max(FR_HEAP_MIN_MB,
                   min(FR_HEAP_MAX_MB, budget_mb * FR_HEAP_SHARE)))

# Escalation ladder for route_auto: each rung re-runs Freerouting on the same
# DSN with more effort. mp = max passes; oit = optimizer improvement threshold
# (percent; lower = keep optimizing longer); us = board update strategy.
DEFAULT_LADDER = [
    {"mp": 20},
    {"mp": 60, "oit": 0.05},
    {"mp": 100, "us": "global"},
]

# Score capture is `\d+(?:\.\d+)?`, NOT `[\d.]+`: when Freerouting finishes with
# nothing unrouted the line has no " (N unrouted)" suffix and ends in a full stop
# ("...score of 997.76."), which `[\d.]+` swallows -> float("997.76.") ValueError.
# The bug fired ONLY on success, because the suffix otherwise terminated the match
# at the space. Found on bb-buck P6 (route probe, completion 1.00).
_PASS_RE = re.compile(
    r"Auto-router pass #(\d+).*?score of (\d+(?:\.\d+)?)(?: \((\d+) unrouted\))?")
_SESSION_RE = re.compile(
    r"Auto-router session completed: started with (\d+) unrouted nets"
    r".*?final score: (\d+(?:\.\d+)?)(?: \((\d+) unrouted\))?")


WORK_MARKER = ".aiee_route_work"


def fresh_work_dir(work: Path) -> Path:
    """(Re)create a work dir SAFELY: only wipe a directory this pipeline
    created (marker file) or an empty one - a user-supplied --work-dir
    pointing at real data must never be rmtree'd (S11 review finding)."""
    import shutil

    work = Path(work)
    if work.exists():
        if not work.is_dir():
            raise CheckError(f"work dir is not a directory: {work}")
        if any(work.iterdir()) and not (work / WORK_MARKER).is_file():
            raise CheckError(
                f"refusing to wipe non-empty work dir {work} (no {WORK_MARKER}"
                " marker; pick a fresh --work-dir)")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / WORK_MARKER).write_text("", encoding="utf-8")
    return work


def swap_in(staged: Path, pcb: Path) -> None:
    """Atomic same-volume swap; degrade to copy for a cross-volume
    --work-dir (os.replace raises OSError across volumes on Windows)."""
    import os
    import shutil

    try:
        os.replace(staged, pcb)
    except OSError:
        shutil.copy2(staged, pcb)
        Path(staged).unlink(missing_ok=True)


def run_worker(bundled_python: Path, job: dict, stage: Path,
               timeout: int = 300, worker: Path | None = None) -> dict:
    """Run one SWIG-worker verb; job/result via files under `stage`.

    Default worker is route_swig; board_update passes lib/update_swig.py
    (same job/result-file protocol - bulk Remove sprays stdout, results
    must travel by file)."""
    tag = uuid.uuid4().hex[:8]
    job_file = stage / f"job_{tag}.json"
    result_file = stage / f"result_{tag}.json"
    job = dict(job)
    job["result"] = str(result_file)
    job_file.write_text(json.dumps(job), encoding="utf-8")
    try:
        # `timeout` stretches on a busy machine, never for a wedged call (#970)
        cp = loadlib.run(
            [str(bundled_python), str(worker or WORKER), str(job_file)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            f"{(worker or WORKER).stem} {job.get('verb')} timed out after "
            f"{exc.timeout:.0f}s (limit {timeout}s; wedged SWIG/wx call - "
            "board untouched)") from exc
    if not result_file.is_file():
        tail = (cp.stderr or cp.stdout or "").strip()[-300:]
        raise CheckError(
            f"{(worker or WORKER).stem} {job.get('verb')} wrote no result "
            f"(exit {cp.returncode}): {tail}")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise CheckError(
            f"{(worker or WORKER).stem} {job.get('verb')} failed: "
            f"{result.get('error')}")
    return result


def build_fr_cmd(java: Path, jar: Path, dsn: Path, ses: Path,
                 rung: dict | None = None) -> list[str]:
    rung = rung or {}
    if jar.name == FR_OVERLAY_JAR:
        launch = ["-cp", os.pathsep.join((str(FR_OVERLAY), str(jar))), FR_MAIN_CLASS]
    else:
        launch = ["-jar", str(jar)]
    heap = fr_heap_mb()
    jvm = ([f"-Xmx{heap}m"] if heap else []) + [FR_OOM_EXIT]
    cmd = [str(java)] + jvm + launch + FR_BASE_FLAGS
    cmd += ["-mp", str(rung.get("mp", 20))]
    if rung.get("oit") is not None:
        cmd += ["-oit", str(rung["oit"])]
    if rung.get("us"):
        cmd += ["-us", str(rung["us"])]
    if rung.get("inc"):
        cmd += ["-inc", str(rung["inc"])]
    cmd += ["-de", str(dsn), "-do", str(ses)]
    return cmd


def run_freerouting(java: Path, jar: Path, dsn: Path, ses: Path, *,
                    rung: dict | None = None, timeout: int = 3600,
                    stall_s: int = 900, cmd: list[str] | None = None,
                    log_file: Path | None = None) -> dict:
    """One Freerouting run. Returns parse_fr_log() facts + process info.

    `timeout` is the HARD wall-clock cap; `stall_s` kills a run that has
    produced no output (and no growth of the .ses) for that long. A 4-layer
    board takes ~5 min per pass, so a run that keeps logging passes is let be
    until the hard cap; a silent wedged JVM ends after `stall_s`."""
    import threading
    import time
    cmd = cmd or build_fr_cmd(java, jar, dsn, ses, rung)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=str(dsn.parent))
    chunks: list[bytes] = []
    last = [time.monotonic()]

    def _pump():
        for line in iter(proc.stdout.readline, b""):
            chunks.append(line)
            last[0] = time.monotonic()

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    start = time.monotonic()
    ses_size = -1
    timed_out = False
    kill_reason = None
    try:
        while proc.poll() is None:
            time.sleep(min(0.2, max(0.01, stall_s / 10)))
            now = time.monotonic()
            try:
                sz = ses.stat().st_size
            except OSError:
                sz = -1
            if sz != ses_size:
                ses_size = sz
                last[0] = max(last[0], now)
            if now - start > timeout:
                kill_reason = "hard"
            elif now - last[0] > stall_s:
                kill_reason = "stall"
            if kill_reason:
                proc.kill()
                timed_out = True
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        t.join(5)
        proc.stdout.close()
    out = b"".join(chunks).decode("utf-8", "replace")
    rc = 124 if timed_out else proc.returncode
    if log_file is not None:
        log_file.write_text(out, encoding="utf-8")
    facts = parse_fr_log(out)
    heap = next((a[4:-1] for a in cmd if a.startswith("-Xmx")), None)
    facts["oom"] = (not timed_out and rc != 0
                    and bool(_OOM_RE.search(out)))
    facts["heap_mb"] = int(heap) if heap and heap.isdigit() else None
    facts.update({"rc": rc, "timed_out": timed_out,
                  "ses_written": ses.is_file(), "cmd": cmd,
                  "kill_reason": kill_reason})
    return facts


def parse_fr_log(text: str) -> dict:
    passes = []
    for m in _PASS_RE.finditer(text):
        passes.append({"pass": int(m.group(1)), "score": float(m.group(2)),
                       "unrouted": int(m.group(3)) if m.group(3) else 0})
    session = _SESSION_RE.search(text)
    unrouted = None
    started = None
    score = None
    if session:
        started = int(session.group(1))
        score = float(session.group(2))
        unrouted = int(session.group(3)) if session.group(3) else None
    if unrouted is None and passes:
        unrouted = passes[-1]["unrouted"]
    if unrouted is None and session:
        unrouted = 0  # session completed, no pass lines captured
    return {"passes": passes, "unrouted": unrouted,
            "started_unrouted": started, "final_score": score,
            "session_completed": bool(session)}


def completion_fraction(facts: dict) -> float | None:
    """Fraction of FR's starting workload that got routed (0..1)."""
    started, left = facts.get("started_unrouted"), facts.get("unrouted")
    if started in (None, 0):
        return 1.0 if facts.get("session_completed") and left in (0, None) \
            else None
    if left is None:
        return None
    return max(0.0, min(1.0, 1.0 - left / started))


def _sexp_end(text: str, i: int) -> int:
    """Index just past the s-expression opening at text[i] == '('."""
    depth, quoted = 0, False
    for j in range(i, len(text)):
        ch = text[j]
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    raise CheckError("unbalanced s-expression in DSN")


def _quote_mask(text: str) -> str:
    """`text` with every character inside double quotes blanked, so a paren
    in a quoted net name ("Net-(C1-Pad2)") is not read as structure."""
    out, quoted = [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            out.append(ch)
        else:
            out.append(" " if quoted else ch)
    return "".join(out)


_DSN_TOKEN_RE = re.compile(r'"[^"]*"|[^\s()]+')


def dsn_apply_net_rules(dsn_text: str, widths: dict[str, float],
                        clearances: dict[str, float]) -> tuple[str, list]:
    """Give every net with a width or clearance floor (mm) above its DSN
    class's own its own class carrying max(class, floor). Returns (text,
    [net, ...] moved). Only a `(resolution um N)` DSN is handled - values are
    in um - anything else comes back unchanged."""
    if not re.search(r"\(resolution\s+um\s+\d+\)", dsn_text):
        return dsn_text, []
    nets = set(widths) | set(clearances)
    out, moved, pos = [], [], 0
    for m in re.finditer(r"\(class\s", dsn_text):
        if m.start() < pos:
            continue
        end = _sexp_end(dsn_text, m.start())
        block = dsn_text[m.start():end]
        head_end = next((j for j, t in enumerate(_quote_mask(block))
                         if j and t == "("), -1)
        head = block[:head_end] if head_end > 0 else block[:-1]
        body = block[head_end:-1] if head_end > 0 else ""
        toks = _DSN_TOKEN_RE.findall(head)[1:]     # drop "class"
        name, members = toks[0], toks[1:]
        cw = re.search(r"\(width\s+([\d.]+)\)", body)
        cc = re.search(r"\(clearance\s+([\d.]+)\)", body)
        cw = float(cw.group(1)) if cw else 0.0
        cc = float(cc.group(1)) if cc else 0.0
        keep, extra = [], []
        for tok in members:
            net = tok.strip('"')
            w = widths.get(net, 0.0) * 1000.0
            c = clearances.get(net, 0.0) * 1000.0
            if net not in nets or (w <= cw and c <= cc):
                keep.append(tok)
                continue
            nb = body
            if w > cw:
                nb = re.sub(r"\(width\s+[\d.]+\)", f"(width {w:g})", nb,
                            count=1)
            if c > cc:
                nb = re.sub(r"\(clearance\s+[\d.]+\)", f"(clearance {c:g})",
                            nb, count=1)
            cname = "aiee_" + re.sub(r"[^A-Za-z0-9_]", "_", net)
            extra.append(f"(class {cname} {tok}\n      {nb.strip()}\n    )")
            moved.append(net)
        out.append(dsn_text[pos:m.start()])
        if keep:
            out.append(f"(class {name} {' '.join(keep)}\n      "
                       f"{body.strip()}\n    )")
        for i, e in enumerate(extra):
            out.append(("\n    " if keep or i else "") + e)
        pos = end
    out.append(dsn_text[pos:])
    return "".join(out), moved


def dru_barriers(dru_text: str) -> list[dict]:
    """rules_gen's aiee_iso_<barrier>_* rules as [{a: [globs], b: [globs],
    clearance_mm}], one per pair of domains, at the larger of its outer and
    inner rules: the DSN holds a pair's clearance for every layer. A part's own
    pad exception isn't a barrier, so it isn't read."""
    pairs: dict[tuple, float] = {}
    for m in re.finditer(r'\(rule\s+"aiee_iso_[^"]*"(.*?)\n\)', dru_text or "", re.S):
        body = m.group(1)
        if "memberOfFootprint" in body:
            continue
        mm = re.search(r"\(constraint\s+clearance\s+\(min\s+([\d.]+)\s*mm\)\)", body)
        a = re.findall(r"A\.NetName == '([^']+)'", body)
        b = re.findall(r"B\.NetName == '([^']+)'", body)
        if mm and a and b:
            key = (tuple(a), tuple(b))
            pairs[key] = max(pairs.get(key, 0.0), float(mm.group(1)))
    return [{"a": list(a), "b": list(b), "clearance_mm": mm}
            for (a, b), mm in pairs.items()]


def dsn_apply_barriers(dsn_text: str, barriers: list[dict]) -> tuple[str, list]:
    """Hold each barrier's two domains apart in the DSN, and only those: a net
    class clearance would hold a net that far from its own domain too. Splits
    each class by the sides its nets are on (rules and vias kept), and adds a
    (class_class (classes X Y) (rule (clearance um))) for every pair across a
    barrier, which Freerouting takes as a pair clearance for tracks, vias and
    pads alike. Globs match as KiCad's == does, case-insensitively, so router
    and DRC see one set of nets. Returns (text, [{classes, clearance_mm}]).
    Only a `(resolution um N)` DSN is handled."""
    if not barriers or not re.search(r"\(resolution\s+um\s+\d+\)", dsn_text):
        return dsn_text, []

    def sides(net: str) -> tuple:
        low = net.lower()
        got = []
        for i, b in enumerate(barriers):
            for side in ("a", "b"):
                if any(fnmatch.fnmatchcase(low, p.lower()) for p in b[side]):
                    got.append((i, side))
        return tuple(got)

    out, pos = [], 0
    members: dict[tuple, list[str]] = {}
    for m in re.finditer(r"\(class\s", dsn_text):
        if m.start() < pos:
            continue
        end = _sexp_end(dsn_text, m.start())
        block = dsn_text[m.start():end]
        head_end = next((j for j, t in enumerate(_quote_mask(block))
                         if j and t == "("), -1)
        head = block[:head_end] if head_end > 0 else block[:-1]
        body = block[head_end:-1] if head_end > 0 else ""
        toks = _DSN_TOKEN_RE.findall(head)[1:]
        name, nets = toks[0], toks[1:]
        groups: dict[tuple, list[str]] = {}
        for tok in nets:
            groups.setdefault(sides(tok.strip('"')), []).append(tok)
        out.append(dsn_text[pos:m.start()])
        parts = []
        for k, (sig, toks_g) in enumerate(sorted(groups.items())):
            cname = name
            if sig:
                tag = "_".join(f"{i}{s}" for i, s in sig)
                cname = re.sub(r"[^A-Za-z0-9_]", "_", name.strip('"')) + f"_iso{tag}"
                members.setdefault(sig, []).append(cname)
            parts.append(f"(class {cname} {' '.join(toks_g)}\n      {body.strip()}\n    )")
        out.append("\n    ".join(parts) if parts else block)
        pos = end
    out.append(dsn_text[pos:])
    text = "".join(out)

    pairs, seen = [], set()
    for i, b in enumerate(barriers):
        um = float(b["clearance_mm"]) * 1000.0
        for sig_a, cls_a in sorted(members.items()):
            if (i, "a") not in sig_a:
                continue
            for sig_b, cls_b in sorted(members.items()):
                if (i, "b") not in sig_b:
                    continue
                for x in cls_a:
                    for y in cls_b:
                        if x != y and (x, y) not in seen:
                            seen.add((x, y))
                            pairs.append({"classes": [x, y], "um": um,
                                          "clearance_mm": float(b["clearance_mm"])})
    if not pairs:
        return dsn_text, []
    clause = "".join(
        f"\n    (class_class (classes {p['classes'][0]} {p['classes'][1]})"
        f"\n      (rule (clearance {p['um']:g}))\n    )" for p in pairs)
    scan = _parser_blanked(text)
    net = re.search(r"\(network[\s)]", _quote_mask(scan))
    if not net:      # a barrier silently dropped is a route through the gap
        raise CheckError("DSN has classes but no (network ...) for the barriers")
    close = _sexp_end(scan, net.start()) - 1
    text = text[:close].rstrip() + clause + "\n  " + text[close:]
    return text, [{"classes": p["classes"], "clearance_mm": p["clearance_mm"]}
                  for p in pairs]


def dsn_apply_layer_widths(dsn_text: str,
                           widths: dict[str, dict[str, float]]) -> tuple[str, list]:
    """Per-layer trace widths (mm) for nets rules_gen sized per stripline
    layer (#489: zrules.layer_geometry): each such net gets its own class
    (unless it is already alone in one) carrying a Specctra
    `(layer_rule <layer> (rule (width W)))` per layer, which Freerouting
    reads into the class's per-layer trace half-width. Layers the DSN does
    not declare are skipped. Returns (text, [net, ...] given layer rules);
    a DSN not in `(resolution um N)` comes back unchanged."""
    if not widths or not re.search(r"\(resolution\s+um\s+\d+\)", dsn_text):
        return dsn_text, []
    declared = set(re.findall(r"\(layer\s+\"?([^\s\"()]+)\"?\s*\(type", dsn_text))
    want = {n: {ly: w for ly, w in m.items() if ly in declared}
            for n, m in widths.items()}
    want = {n: m for n, m in want.items() if m}
    if not want:
        return dsn_text, []

    def rules_for(net: str) -> str:
        return "".join(f"\n      (layer_rule {ly}\n        (rule (width "
                       f"{w * 1000.0:g}))\n      )"
                       for ly, w in sorted(want[net].items()))

    out, done, pos = [], [], 0
    for m in re.finditer(r"\(class\s", dsn_text):
        if m.start() < pos:
            continue
        end = _sexp_end(dsn_text, m.start())
        block = dsn_text[m.start():end]
        head_end = next((j for j, t in enumerate(_quote_mask(block))
                         if j and t == "("), -1)
        head = block[:head_end] if head_end > 0 else block[:-1]
        body = block[head_end:-1] if head_end > 0 else ""
        toks = _DSN_TOKEN_RE.findall(head)[1:]
        name, members = toks[0], toks[1:]
        hit = [t for t in members if t.strip('"') in want]
        out.append(dsn_text[pos:m.start()])
        pos = end
        if not hit:
            out.append(block)
            continue
        keep = [t for t in members if t not in hit]
        parts = []
        if keep:
            parts.append(f"(class {name} {' '.join(keep)}\n      "
                         f"{body.strip()}\n    )")
        for tok in hit:
            net = tok.strip('"')
            cname = name if not keep and len(hit) == 1 else \
                "aiee_" + re.sub(r"[^A-Za-z0-9_]", "_", net)
            parts.append(f"(class {cname} {tok}\n      {body.strip()}"
                         f"{rules_for(net)}\n    )")
            done.append(net)
        out.append("\n    ".join(parts))
    out.append(dsn_text[pos:])
    return "".join(out), done


_DSN_SEXP_TOKEN_RE = re.compile(r'"[^"]*"|[()]|[^\s()"]+')
_DSN_HEAD_RE = re.compile(r"\(\s*([^\s()]+)")


def _child_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """(start, end) of every direct child of the s-expression at
    text[start:end]."""
    masked = _quote_mask(text[start:end])
    spans, j = [], 1                      # skip the parent's own "("
    while j < len(masked) - 1:
        if masked[j] == "(":
            e = _sexp_end(text, start + j) - start
            spans.append((start + j, start + e))
            j = e
        else:
            j += 1
    return spans


def _sexp_head(text: str, s: int, e: int) -> str:
    m = _DSN_HEAD_RE.match(_quote_mask(text[s:e]))
    return m.group(1) if m else ""


def _parse_dsn_wire(text: str, s: int, e: int) -> dict | None:
    """One `(wire (path LAYER WIDTH x y x y ...) attrs...)`; None for any
    other shape (polygon, qarc, a nested paren in the path, an odd or
    non-numeric coordinate list)."""
    kids = _child_spans(text, s, e)
    if not kids or _quote_mask(text[s:kids[0][0]]).split() != ["(wire"]:
        return None
    ps, pe = kids[0]
    inner = text[ps + 1:pe - 1]
    if "(" in _quote_mask(inner):
        return None
    toks = _DSN_TOKEN_RE.findall(inner)
    if len(toks) < 7 or toks[0] != "path" or (len(toks) - 3) % 2:
        return None
    pts = [(toks[k], toks[k + 1]) for k in range(3, len(toks), 2)]
    try:
        fpts = [(float(x), float(y)) for x, y in pts]
    except ValueError:
        return None
    # attrs = everything after the path ((net ..)(type protect)...), as a
    # token tuple: two wires join only when they agree on all of it
    attrs = tuple(_DSN_SEXP_TOKEN_RE.findall(text[pe:e - 1]))
    return {"span": (s, e), "path": (ps, pe), "layer": toks[1],
            "width": toks[2], "key": (toks[1], toks[2], attrs),
            "pts": pts, "fpts": fpts}


def _cut_span(text: str, s: int, e: int) -> tuple[int, int]:
    """Widen a span to its whole line when nothing else shares that line."""
    ls, le = s, e
    while ls > 0 and text[ls - 1] in " \t":
        ls -= 1
    while le < len(text) and text[le] in " \t\r":
        le += 1
    if (ls == 0 or text[ls - 1] == "\n") and le < len(text) \
            and text[le] == "\n":
        return ls, le + 1
    return s, e


def dsn_merge_wires(dsn_text: str) -> tuple[str, int]:
    """Join each net's pre-routed wire chains into one `(path ...)` per chain.

    KiCad's DSN export writes hand-routed copper as many short wires, and
    Freerouting 2.2.4 overflows its stack in PolylineTrace.combine reading
    them (PCB-0019 USB D+). Two wires join at an endpoint only when exactly
    those two wires on that layer end there, no via sits on it, and they
    agree on layer, width and every attribute (net, type protect/fix, ...).
    A branch point, a via, a cycle or a chain that closes on itself stays
    as exported; coordinates keep their original strings. Returns (text,
    input wires folded into a longer one); unparseable or unbalanced input
    comes back unchanged with 0."""
    # KiCad's parser header declares `(string_quote ")`: that lone quote
    # would flip every quote-aware scan after it. Parse a same-length copy
    # with it blanked; edits go back onto the original by index.
    scan = re.sub(r'\(string_quote\s+"\s*\)',
                  lambda q: q.group(0).replace('"', " "), dsn_text)
    m = re.search(r"\(wiring[\s)]", _quote_mask(scan))
    if not m:
        return dsn_text, 0
    wires, vias = [], set()
    try:
        top = scan.index("(")
        if scan[_sexp_end(scan, top):].strip():
            return dsn_text, 0                  # trailing junk / 2nd tree
        wend = _sexp_end(scan, m.start())
        for s, e in _child_spans(scan, m.start(), wend):
            head = _sexp_head(scan, s, e)
            if head == "wire":
                w = _parse_dsn_wire(scan, s, e)
                if w:
                    wires.append(w)
            elif head == "via":
                # (via PADSTACK x y (net ..)...) - a via ends any chain
                cut = _quote_mask(scan[s + 1:e]).find("(")
                stop = s + 1 + cut if cut >= 0 else e - 1
                toks = _DSN_TOKEN_RE.findall(scan[s + 1:stop])
                try:
                    vias.add((float(toks[2]), float(toks[3])))
                except (IndexError, ValueError):
                    pass
    except CheckError:
        return dsn_text, 0

    # every parsed endpoint counts toward its node's degree; only wires
    # with distinct ends are chain material
    deg: dict = {}
    at: dict = {}
    for i, w in enumerate(wires):
        for end in (w["fpts"][0], w["fpts"][-1]):
            node = (w["layer"], end)
            deg[node] = deg.get(node, 0) + 1
            if w["fpts"][0] != w["fpts"][-1]:
                at.setdefault(node, []).append(i)

    def through(i: int, pt) -> int | None:
        """The wire a chain continues into from wire i at pt, if any."""
        node = (wires[i]["layer"], pt)
        ws = at.get(node, [])
        if deg[node] != 2 or len(ws) != 2 or pt in vias or ws[0] == ws[1]:
            return None
        j = ws[1] if ws[0] == i else ws[0]
        return j if wires[j]["key"] == wires[i]["key"] else None

    used: set[int] = set()
    chains = []
    for i, w in enumerate(wires):
        if i in used or w["fpts"][0] == w["fpts"][-1]:
            continue
        used.add(i)
        chain = [(i, False)]                    # (wire, reversed?)
        cyclic = False
        for forward in (True, False):
            prev, cur = i, w["fpts"][-1 if forward else 0]
            while not cyclic and (j := through(prev, cur)) is not None:
                if j in used:                   # walked back round to i
                    cyclic = True
                    break
                used.add(j)
                f = wires[j]["fpts"]
                starts_here = f[0] == cur
                # forward: j must start at cur; backward: j must end there
                rev = not starts_here if forward else starts_here
                chain.insert(len(chain) if forward else 0, (j, rev))
                prev, cur = j, f[-1] if starts_here else f[0]
        if cyclic or len(chain) < 2:
            continue
        pts: list = []
        for j, rev in chain:
            seq = wires[j]["pts"][::-1] if rev else wires[j]["pts"]
            pts.extend(seq[1:] if pts else seq)
        if tuple(map(float, pts[0])) == tuple(map(float, pts[-1])):
            continue                            # closes on itself
        chains.append(([j for j, _ in chain], pts))

    edits = []                                  # (start, end, replacement)
    merged = 0
    for members, pts in chains:
        members.sort(key=lambda j: wires[j]["span"][0])
        keep = wires[members[0]]
        path = "(path %s %s  %s)" % (keep["layer"], keep["width"],
                                     "  ".join(f"{x} {y}" for x, y in pts))
        edits.append((*keep["path"], path))
        for j in members[1:]:
            edits.append((*_cut_span(dsn_text, *wires[j]["span"]), ""))
        merged += len(members)
    if not merged:
        return dsn_text, 0
    out, pos = [], 0
    for s, e, rep in sorted(edits):
        out.append(dsn_text[pos:s])
        out.append(rep)
        pos = e
    out.append(dsn_text[pos:])
    return "".join(out), merged


_VIA_PADSTACK_RE = re.compile(
    r'\(padstack\s+"?(Via\[[^\]]*\]_([\d.]+):([\d.]+)_um)"?')


def _structure_span(scan: str) -> tuple[int, int] | None:
    m = re.search(r"\(structure[\s)]", _quote_mask(scan))
    if not m:
        return None
    return m.start(), _sexp_end(scan, m.start())


def _parser_blanked(text: str) -> str:
    """Same-length copy with the parser header's lone `(string_quote ")`
    quote blanked (see dsn_merge_wires)."""
    return re.sub(r'\(string_quote\s+"\s*\)',
                  lambda q: q.group(0).replace('"', " "), text)


def dsn_class_clearances(dsn_text: str) -> list[float]:
    """Every clearance (um) the DSN's rules set without a `(type ...)`:
    the structure's default and each class's own."""
    return [float(m.group(1)) for m in
            re.finditer(r"\(clearance\s+([\d.]+)\s*\)", dsn_text)]


def dsn_layer_names(dsn_text: str) -> list[str]:
    """Copper layers of a DSN, in stack order."""
    scan = _parser_blanked(dsn_text)
    span = _structure_span(scan)
    if span is None:
        return []
    return re.findall(r'\(layer\s+"?([^\s")]+)"?\s*\n?\s*\(type',
                      scan[span[0]:span[1]])


def span_padstack(i: int, j: int, size_mm: float, drill_mm: float) -> str:
    """KiCad's DSN name for a via on copper i..j: its SES import reads the
    span from the shapes and the drill from this name."""
    return f"Via[{i}-{j}]_{size_mm * 1000:.6g}:{drill_mm * 1000:.6g}_um"


def dsn_add_spans(dsn_text: str, spans: list[tuple[int, int, float, float]]
                  ) -> tuple[str, list[str]]:
    """Offer the router an HDI build's via spans (#477). KiCad's DSN declares
    the netclass's through via and the vias already on the board, so each span
    (i, j, size mm, drill mm) gets a padstack in the library, a place in the
    structure's (via ...) list and in each class's use_via. Returns (text,
    padstack names added). The spans go ahead of the through via: Freerouting
    2.2.4 takes these lists in order, and with the spans behind it placed no
    span at all (UFBGA-64 fixture: 0 of 32 vias; ahead, 14 of 44 and six fewer
    nets left unrouted). The class's own via stays usable, last."""
    layers = dsn_layer_names(dsn_text)
    if not layers or "(unit um)" not in dsn_text:
        return dsn_text, []
    names, blocks = [], []
    have = set(re.findall(r'\(padstack\s+"?(Via\[[^\]]*\]_[\d.]+:[\d.]+_um)"?',
                          dsn_text))
    every = []
    for i, j, size, drill in spans:
        name = span_padstack(i, j, size, drill)
        every.append(name)
        if name in have:
            continue
        names.append(name)
        shapes = "".join(f"      (shape (circle {layers[k]} {size * 1000:.6g}))\n"
                         for k in range(i, j + 1))
        blocks.append(f'    (padstack "{name}"\n{shapes}      (attach off)\n    )\n')
    lib = re.search(r'\(library\s*\n', dsn_text)
    if lib is None:
        return dsn_text, []
    text = dsn_text[:lib.end()] + "".join(blocks) + dsn_text[lib.end():]

    def extend(m):
        got = re.findall(r'"([^"]+)"', m.group(2))
        rest = [n for n in got if n not in every]
        return m.group(1) + " ".join(f'"{n}"' for n in every + rest) + m.group(3)

    text = re.sub(r'(\(via\s+)("[^"]*_um"(?:\s+"[^"]*_um")*)(\s*\))', extend, text, count=1)
    text = re.sub(r'(\(use_via\s+)("[^"]*_um"(?:\s+"[^"]*_um")*)(\s*\))', extend, text)
    return text, names


def dsn_hole_floor(dsn_text: str, hole_to_hole_mm: float,
                   span_floor_mm: float | None = None
                   ) -> tuple[str, dict[str, tuple[float, float]]]:
    """Make Freerouting keep vias of different nets JLC's hole-to-hole
    floor apart. Freerouting spaces vias by copper clearance only, so two
    0.6/0.3 vias at the 0.127 mm floor land 0.727 mm apart, 0.427 mm drill
    edge to drill edge, under KiCad's 0.5 mm hole_to_hole (lite-main,
    hifi-receiver-2: four drc_routed errors after every route). Each KiCad
    via padstack (`Via[0-1]_<dia>:<drill>_um`) has its copper circle grown
    to hole_to_hole + drill - the least DSN clearance, so copper clearance
    alone keeps the drills apart. The padstack name keeps the real sizes;
    ses_restore_vias puts the real diameter back before import. Returns
    (text, {padstack: (real_um, grown_um)}); anything not in um comes back
    unchanged. span_floor_mm: on an HDI build, the floor for a padstack that
    isn't through (a laser hole isn't drilled with the through vias, #477);
    None leaves every padstack on hole_to_hole_mm."""
    if not re.search(r"\(resolution\s+um\s+\d+\)", dsn_text) \
            or not re.search(r"\(unit\s+um\)", dsn_text):
        return dsn_text, {}
    clrs = dsn_class_clearances(dsn_text)
    if not clrs:
        return dsn_text, {}
    c_min = min(clrs)
    grown: dict[str, tuple[float, float]] = {}
    out, pos = [], 0
    through = f"Via[0-{len(dsn_layer_names(dsn_text)) - 1}]_"
    for m in _VIA_PADSTACK_RE.finditer(dsn_text):
        name, dia, drill = m.group(1), float(m.group(2)), float(m.group(3))
        floor = hole_to_hole_mm
        if span_floor_mm is not None and not name.startswith(through):
            floor = span_floor_mm
        want = round(floor * 1000.0 + drill - c_min, 1)
        if want <= dia + 1e-6:
            continue
        end = _sexp_end(dsn_text, m.start())
        block = dsn_text[m.start():end]
        nb, n = re.subn(r"\(circle\s+(\S+)\s+([\d.]+)\s*\)",
                        lambda c: f"(circle {c.group(1)} {want:g})", block)
        if not n:
            continue
        out.append(dsn_text[pos:m.start()])
        out.append(nb)
        pos = end
        grown[name] = (dia, want)
    out.append(dsn_text[pos:])
    return "".join(out), grown


def ses_restore_vias(ses_text: str, grown: dict[str, tuple[float, float]]
                     ) -> tuple[str, int]:
    """Undo dsn_hole_floor in a session file: each grown via padstack in
    the SES's library_out gets its real diameter back (KiCad's SES import
    sizes a via from that circle and its drill from the padstack name).
    Returns (text, padstacks restored)."""
    if not grown:
        return ses_text, 0
    m = re.search(r"\(resolution\s+um\s+(\d+)\)", ses_text)
    scale = float(m.group(1)) if m else 1.0
    out, pos, n = [], 0, 0
    for pm in re.finditer(r'\(padstack\s+"?(Via\[[^\]"]*\]_[\d.]+:[\d.]+_um)"?',
                          ses_text):
        real = grown.get(pm.group(1))
        if real is None:
            continue
        end = _sexp_end(ses_text, pm.start())
        block = ses_text[pm.start():end]
        dia = real[0] * scale
        nb = re.sub(r"\(circle\s+(\S+)\s+[\d.]+",
                    lambda c: f"(circle {c.group(1)} {dia:g}", block)
        out.append(ses_text[pos:pm.start()])
        out.append(nb)
        pos = end
        n += 1
    out.append(ses_text[pos:])
    return "".join(out), n


def _slot_pts(cx: float, cy: float, w: float, h: float, angle_deg: float,
              grow: float, segs: int = 8) -> list[tuple[float, float]]:
    """Outline (mm, board frame) of a w x h slot grown by `grow`, turned by
    -angle like geom.Pad.drill_poly; its arcs are circumscribed, so the
    polygon covers the true stadium."""
    import math
    r = (min(w, h) / 2.0 + grow) / math.cos(math.pi / (2 * segs))
    half = abs(w - h) / 2.0
    pts = []
    for end, a0 in ((1, -90.0), (-1, 90.0)):       # long axis along x
        for k in range(segs + 1):
            a = math.radians(a0 + 180.0 * k / segs)
            pts.append((end * half + r * math.cos(a), r * math.sin(a)))
    t = math.radians(-angle_deg + (0.0 if w >= h else 90.0))
    ct, st = math.cos(t), math.sin(t)
    return [(cx + x * ct - y * st, cy + x * st + y * ct) for x, y in pts]


def dsn_npth_keepouts(dsn_text: str, holes: list[dict], margin_mm: float
                      ) -> tuple[str, int]:
    """Put a keepout on every signal layer over each NPTH hole, grown by
    `margin_mm` (the board's hole clearance or copper-to-edge rule,
    whichever is larger: KiCad grades an NPTH hole by both). KiCad's DSN
    carries an NPTH as a footprint keepout at the bare hole, or, when the
    pad has copper layers, as a netless pin of the hole's size; either way
    Freerouting keeps only its copper clearance from it, and on lite-main
    it routed 0.16 mm from the RCA jacks' locating pegs. holes: [{"at":
    [x, y], "drill": [w, h], "angle": deg}] in board mm. Returns (text,
    keepouts added); anything not in um comes back unchanged."""
    if not holes or not re.search(r"\(unit\s+um\)", dsn_text):
        return dsn_text, 0
    scan = _parser_blanked(dsn_text)
    span = _structure_span(scan)
    if span is None:
        return dsn_text, 0
    s, e = span
    layers = re.findall(r"\(layer\s+(\S+)\s+\(type\s+(?:signal|power|mixed)\)",
                        scan[s:e])
    if not layers:
        return dsn_text, 0
    lines = []
    for h in holes:
        w, hh = (float(v) for v in h["drill"])
        x, y = float(h["at"][0]), float(h["at"][1])
        if abs(w - hh) < 1e-6:
            d = (w + 2 * margin_mm) * 1000
            shapes = [f"(circle {ly} {d:.1f} {x * 1000:.1f} {-y * 1000:.1f})"
                      for ly in layers]
        else:
            pts = _slot_pts(x, y, w, hh, float(h.get("angle", 0.0)),
                            margin_mm)
            pts.append(pts[0])
            coords = "  ".join(f"{px * 1000:.1f} {-py * 1000:.1f}"
                               for px, py in pts)
            shapes = [f"(polygon {ly} 0  {coords})" for ly in layers]
        lines += [f'    (keepout "aiee_npth" {sh})' for sh in shapes]
    # insert just before the structure's closing paren
    close = e - 1
    text = dsn_text[:close].rstrip() + "\n" + "\n".join(lines) + "\n  " \
        + dsn_text[close:]
    return text, len(holes)


def edge_segments(rings, tol_mm: float = 0.01) -> list[tuple]:
    """Each board-edge segment once: rings are point lists (mm) of the
    outline faces, exterior and windows alike (a ring is simplified to
    `tol_mm` first, so an arc's many chords collapse). Returns
    [((x0, y0), (x1, y1)), ...]."""
    from shapely.geometry import LineString
    seen, out = set(), []
    for ring in rings:
        pts = list(ring)
        if len(pts) < 2:
            continue
        line = LineString(pts)
        if tol_mm > 0:
            line = line.simplify(tol_mm, preserve_topology=False)
        cs = list(line.coords)
        for a, b in zip(cs, cs[1:]):
            if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                continue
            key = tuple(sorted(((round(a[0], 4), round(a[1], 4)),
                               (round(b[0], 4), round(b[1], 4)))))
            if key not in seen:
                seen.add(key)
                out.append(((a[0], a[1]), (b[0], b[1])))
    return out


def dsn_edge_keepouts(dsn_text: str, segments: list[tuple], margin_mm: float
                      ) -> tuple[str, int]:
    """Put a keepout on every signal layer along each board-edge segment:
    a rectangle `margin_mm` either side of it, run on `margin_mm` past
    each end so corners stay covered. KiCad grades copper by
    min_copper_edge_clearance from Edge.Cuts; the DSN boundary carries no
    such rule, so Freerouting keeps only its copper clearance from the
    edge (ultra-front: tracks 0.28 mm from it against a 0.3 mm rule).
    segments: edge_segments() output in board mm. Returns (text,
    keepouts added); with no margin, or anything not in um, the text
    comes back unchanged."""
    import math
    if margin_mm <= 0 or not segments \
            or not re.search(r"\(unit\s+um\)", dsn_text):
        return dsn_text, 0
    scan = _parser_blanked(dsn_text)
    span = _structure_span(scan)
    if span is None:
        return dsn_text, 0
    s, e = span
    layers = re.findall(r"\(layer\s+(\S+)\s+\(type\s+(?:signal|power|mixed)\)",
                        scan[s:e])
    if not layers:
        return dsn_text, 0
    m = margin_mm
    lines = []
    for (x0, y0), (x1, y1) in segments:
        ln = math.hypot(x1 - x0, y1 - y0)
        dx, dy = (x1 - x0) / ln, (y1 - y0) / ln
        nx, ny = -dy, dx
        a = (x0 - dx * m, y0 - dy * m)
        b = (x1 + dx * m, y1 + dy * m)
        pts = [(a[0] + nx * m, a[1] + ny * m), (b[0] + nx * m, b[1] + ny * m),
               (b[0] - nx * m, b[1] - ny * m), (a[0] - nx * m, a[1] - ny * m)]
        pts.append(pts[0])
        coords = "  ".join(f"{px * 1000:.1f} {-py * 1000:.1f}"
                           for px, py in pts)
        lines += [f'    (keepout "aiee_edge" (polygon {ly} 0  {coords}))'
                  for ly in layers]
    close = e - 1
    text = dsn_text[:close].rstrip() + "\n" + "\n".join(lines) + "\n  " \
        + dsn_text[close:]
    return text, len(segments)


def _shuffle_children(text: str, scan: str, head: str, child: str,
                      rng) -> str:
    """Shuffle the order of the `(child ...)` children of the first
    `(head ...)` section; every other child keeps its place."""
    m = re.search(r"\(" + head + r"[\s)]", _quote_mask(scan))
    if not m:
        return text
    end = _sexp_end(scan, m.start())
    spans = [sp for sp in _child_spans(scan, m.start(), end)
             if _sexp_head(scan, *sp) == child]
    if len(spans) < 2:
        return text
    blocks = [text[s:e] for s, e in spans]
    order = list(range(len(blocks)))
    rng.shuffle(order)
    out, pos = [], 0
    for (s, e), k in zip(spans, order):
        out.append(text[pos:s])
        out.append(blocks[k])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def dsn_shuffle(dsn_text: str, seed: int) -> str:
    """The same design with its placement components and network nets in a
    seeded random order. Freerouting's sequential router is deterministic
    for one DSN but takes items in file order, so each order is another
    route of the same board (route_auto --tries). Same length, same
    content; unparseable input comes back unchanged."""
    import random
    rng = random.Random(seed)
    try:
        text = dsn_text
        for head, child in (("placement", "component"), ("network", "net")):
            text = _shuffle_children(text, _parser_blanked(text), head,
                                     child, rng)
        return text
    except CheckError:
        return dsn_text
