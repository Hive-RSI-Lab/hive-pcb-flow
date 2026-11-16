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
from dataclasses import dataclass, field
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
# Why the ceiling is 1536 and not 4096: the JVM fills whatever -Xmx allows
# before a full GC, so the cap, not the board, sets its size. On a 6-layer
# two-BGA bench (2026-11-15) the heap reached 2783 MB, a full GC left 157 MB
# live, and the job's tree peaked at 4.3 GB, past heavy's 4500 MB job
# reservation once Xvfb and python are added; memguard killed three such runs.
# 1536 is ~10x that live set; full GCs come every few minutes at ~0.5 s each.
FR_HEAP_SHARE = 0.5     # the rest is the JVM's own overhead and our process
FR_HEAP_MIN_MB = 512
FR_HEAP_MAX_MB = 1536
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
    r"Auto-router session completed( with timeout)?: started with (\d+) "
    r"unrouted nets"
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


def fr_job_timeout(timeout_s: float) -> int:
    """Freerouting's own stop, ahead of run_freerouting's hard kill: at
    --router.job_timeout it ends the session and writes the SES so far
    (2.2.4: "session completed with timeout", rc 0), where the kill leaves
    no SES at all (an escaped 6-layer BGA bench: every rung
    killed at 600 s while still improving). A tenth of the cap, at least
    20 s, is left for the SES write."""
    return max(10, int(timeout_s - max(20, timeout_s / 10)))


def build_fr_cmd(java: Path, jar: Path, dsn: Path, ses: Path,
                 rung: dict | None = None,
                 job_timeout_s: int | None = None) -> list[str]:
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
    if job_timeout_s:
        h, rest = divmod(int(job_timeout_s), 3600)
        cmd += [f"--router.job_timeout={h:02d}:{rest // 60:02d}:{rest % 60:02d}"]
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
    cmd = cmd or build_fr_cmd(java, jar, dsn, ses, rung,
                              job_timeout_s=fr_job_timeout(timeout))
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
        started = int(session.group(2))
        score = float(session.group(3))
        unrouted = int(session.group(4)) if session.group(4) else None
    if unrouted is None and passes:
        unrouted = passes[-1]["unrouted"]
    if unrouted is None and session:
        unrouted = 0  # session completed, no pass lines captured
    return {"passes": passes, "unrouted": unrouted,
            "started_unrouted": started, "final_score": score,
            "session_completed": bool(session),
            "job_timeout_hit": bool(session and session.group(1))}


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


# one token of a .kicad_dru as KiCad's DSN lexer reads it: a line whose first
# non-blank character is '#' is a comment, a "..." string takes backslash
# escapes and ends at the end of its line if left open, and a paren inside a
# string is text
_DRU_TOKEN_RE = re.compile(
    r'(?P<comment>^[ \t\r\f\v]*#[^\n]*)'
    r'|(?P<str>"(?:[^"\\\n]|\\.)*"?)'
    r'|(?P<open>\()|(?P<close>\))'
    r'|(?P<text>[^()"\n]+|\n)', re.M)


def dru_forms(dru_text: str) -> list[str]:
    """The top-level (...) forms of a .kicad_dru in file order, each as its
    own text with its comment lines dropped (#1163). Parens are balanced as
    KiCad reads them, so a rule may close anywhere: on a line of its own in
    column 0 (as rules_gen writes), indented, or at the end of a one-line
    rule (as KiCad's own help and eda_io write). A form left open runs to the
    end of the file; a stray ')' outside any form is dropped."""
    forms: list[str] = []
    buf: list[str] = []
    depth = 0
    for m in _DRU_TOKEN_RE.finditer(dru_text or ""):
        kind, tok = m.lastgroup, m.group()
        if kind == "comment":
            continue
        if kind == "open":
            depth += 1
        elif kind == "close":
            if not depth:
                continue
            depth -= 1
        if depth or kind == "close":
            buf.append(tok)
            if not depth:
                forms.append("".join(buf))
                buf = []
    if buf:
        forms.append("".join(buf))
    return forms


# KiCad 10 reads each (min/opt/max ...) value of a rule through its
# expression evaluator, and one value it rejects is a parse error that drops
# the WHOLE .kicad_dru while kicad-cli says nothing (#1273). What follows was
# read from its drc_rule_parser.cpp and measured on 10.0.6: a length takes mm,
# mil or in, spelled exactly so (a blank before the unit is fine); 20mil and
# 0.02in hold at 0.508 mm, and 0.5, 500um, 0.05cm, 0.5MM, 20mils, 1e-2mm and
# .5mm each void the file.
DRU_LENGTH_UNITS = {"mm": 1.0, "mil": 0.0254, "in": 25.4}
# every unit it reads, by kind: an angle and two delays besides the lengths
DRU_UNIT_KINDS = {"mm": "length", "mil": "length", "in": "length",
                  "deg": "angle", "fs": "time", "ps": "time"}
# the constraint types whose values it reads that way; assertion, disallow,
# zone_connection and min_resolved_spokes are written in other forms
DRU_VALUE_TYPES = frozenset((
    "annular_width", "bridged_mask", "clearance", "connection_width",
    "courtyard_clearance", "creepage", "diff_pair_gap", "diff_pair_uncoupled",
    "edge_clearance", "hole_clearance", "hole_size", "hole_to_hole", "length",
    "physical_clearance", "physical_hole_clearance", "silk_clearance", "skew",
    "solder_mask_expansion", "solder_mask_sliver", "solder_paste_abs_margin",
    "solder_paste_rel_margin", "text_height", "text_thickness",
    "thermal_relief_gap", "thermal_spoke_width", "track_angle",
    "track_segment_length", "track_width", "via_count", "via_dangling",
    "via_diameter"))
# old type names it still takes, as the type each stands for
DRU_TYPE_ALIASES = {"hole": "hole_size",
                    "mechanical_clearance": "physical_clearance",
                    "mechanical_hole_clearance": "physical_hole_clearance"}
# types whose values are plain numbers: a unit on one voids the file, and a
# bare number is fine (on every other type a bare number voids it)
DRU_UNITLESS_TYPES = frozenset(("bridged_mask", "track_angle", "via_count",
                                "via_dangling"))
# the only types a delay (fs, ps) may be given for
DRU_TIME_TYPES = frozenset(("length", "skew"))
# the kind of a value is the kind of its last unit, an angle or no unit at
# all reading as unitless; each value of a clause must be of the kind of the
# one before it (#1309)
_DRU_KIND_NAMES = {"length": "a length", "time": "a delay",
                   "unitless": "a unitless value"}
# a value's own tokens: a number (a comma reads as a point), a word, an
# operator or paren, a run of blanks, or any other character; ASCII only, as
# KiCad's evaluator reads no other digit or letter
_DRU_EXPR_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d*)?)|(?P<word>[^\W\d]\w*)|(?P<op>[-+*/()])"
    r"|(?P<blank> +)|(?P<other>.)", re.S | re.ASCII)
# the first token of a list: its keyword, bare or quoted. A bare one stops at
# a form feed or vertical tab too, leaving it in the value to be flagged:
# KiCad's lexer reads neither as a blank, so (min\f0.1mm) voids the file
_DRU_HEAD_RE = re.compile(
    r'[ \t\r\n]*(?:"(?:[^"\\\n]|\\.)*"?|[^ \t\r\n\f\v()"]+)')


class DruList(list):
    """One (...) of a form read by dru_tree: its items, and as .raw the text
    between its parens as written (#1273)."""
    __slots__ = ("raw",)

    def __init__(self, raw: str = ""):
        super().__init__()
        self.raw = raw


def dru_tree(form: str) -> list:
    """One form of dru_forms as nested lists, read with the same lexer: a
    (...) is a DruList, a "..." string one atom without its quotes and
    escapes, and other text splits at blanks. A list left open closes at
    the end of the form (#1273)."""
    form = form or ""
    stack: list[tuple[DruList, int]] = [(DruList(), 0)]
    for m in _DRU_TOKEN_RE.finditer(form):
        kind, tok = m.lastgroup, m.group()
        if kind == "open":
            stack.append((DruList(), m.end()))
        elif kind == "close" and len(stack) > 1:
            done, start = stack.pop()
            done.raw = form[start:m.start()]
            stack[-1][0].append(done)
        elif kind == "str":
            body = tok[1:-1] if len(tok) > 1 and tok.endswith('"') else tok[1:]
            stack[-1][0].append(re.sub(r"\\(.)", r"\1", body))
        elif kind == "text":
            stack[-1][0].extend(tok.split())
    while len(stack) > 1:
        done, start = stack.pop()
        done.raw = form[start:]
        stack[-1][0].append(done)
    return next((x for x in stack[0][0] if isinstance(x, list)), DruList())


def dru_rule_constraints(form: str) -> tuple[str, list] | None:
    """A (rule NAME ...) form -> (NAME, [(TYPE, [(KEY, VALUE), ...])]): one
    entry per (constraint TYPE ...) clause in file order, an old type name
    read as the one it stands for (hole is hole_size), and each (min|opt|max
    VALUE) of the clause in its own order with VALUE as written, blanks
    inside it kept. KiCad holds every clause of a rule and checks min and
    max wherever they sit beside opt (#1273). None for a form that is not a
    rule."""
    tree = dru_tree(form)
    if not tree or tree[0] != "rule":
        return None
    name = tree[1] if len(tree) > 1 and isinstance(tree[1], str) else ""
    out = []
    for node in tree[2:]:
        if not (isinstance(node, list) and len(node) > 1
                and node[0] == "constraint" and isinstance(node[1], str)):
            continue
        vals = []
        for sub in node[2:]:
            if isinstance(sub, DruList) and sub and sub[0] in ("min", "opt",
                                                               "max"):
                head = _DRU_HEAD_RE.match(sub.raw)
                rest = sub.raw[head.end():] if head else sub.raw
                vals.append((sub[0], rest.strip(" \t\r\n")))
        out.append((DRU_TYPE_ALIASES.get(node[1], node[1]), vals))
    return name, out


def _dru_wrapped(toks: list) -> bool:
    """Whether a value's tokens are one (...) from first to last."""
    depth = 0
    for i, tok in enumerate(toks):
        if tok == ("op", "("):
            depth += 1
        elif tok == ("op", ")"):
            depth -= 1
            if depth <= 0:
                return depth == 0 and i == len(toks) - 1
    return False


def _dru_expr_error(toks: list) -> str | None:
    """Why KiCad 10's evaluator cannot parse a value made of numbers and
    + - * / ( ), each number's unit taken off: a sign may stand before any
    number or '(', and every other operator goes between two values. None
    for one it parses (#1309)."""
    depth, want_value = 0, True
    for kind, tok in toks:
        if want_value:
            if kind == "num":
                want_value = False
            elif tok == "(":
                depth += 1
            elif tok not in ("+", "-"):
                return f"has {tok!r} where a number goes"
        elif kind == "num" or tok == "(":
            return "has two values with no operator between them"
        elif tok == ")":
            if not depth:
                return "has a ')' with no '(' before it"
            depth -= 1
        else:
            want_value = True
    if not toks:
        return "has no value"
    if want_value:
        return f"ends in {toks[-1][1]!r}"
    if depth:
        return "has a '(' that is not closed"
    return None


def _dru_scan(value: str | None, ctype: str) -> tuple:
    """One value of a `ctype` constraint as KiCad 10's rule parser reads it
    -> (mm, kind, why): mm for a plain length (20mil, -0.1mm, (0.2 mm)); its
    kind, the kind of its last unit (length or time), an angle or no unit at
    all reading as unitless, as 1mm + 1deg does; and why KiCad rejects it.
    kind is None for a value it rejects and where this reader cannot follow
    the value (a string, a name, a character it does not know), and it then
    gives no verdict (#1273, #1309)."""
    text = (value or "").strip(" \t\r\n")
    if not text:
        return None, None, "has no value"
    if '"' in text or "'" in text:
        return None, None, None
    if re.search(r"[\f\v]", text):
        return None, None, ("has a form feed or vertical tab, which KiCad "
                            "does not read as a blank")
    if re.search(r"[\t\n\r]", text):
        return None, None, ("has a tab or line break inside, which KiCad's "
                            "evaluator rejects")
    if re.search(r"(?<!\w)\.\d", text):
        return None, None, ("has a number with no digit before its point "
                            "(write 0.5, not .5)")
    toks = [(m.lastgroup, m.group()) for m in _DRU_EXPR_RE.finditer(text)
            if m.lastgroup != "blank"]
    unitless = ctype in DRU_UNITLESS_TYPES
    kind, unit, follow = "unitless", None, True
    for i, (cls, tok) in enumerate(toks):
        before = toks[i - 1] if i else ("", "")
        if cls == "other":
            follow = False
        elif cls != "word":
            continue
        elif before[0] == "num":
            if re.match(r"[eE](\d|$)", tok):
                return None, None, "has an exponent, which KiCad does not read"
            if unitless:
                return None, None, (f"has unit {tok!r}, but {ctype} takes a "
                                    "plain number")
            if tok not in DRU_UNIT_KINDS:
                return None, None, (f"has unit {tok!r}, which KiCad does not "
                                    "read (mm, mil, in)")
            kind, unit = DRU_UNIT_KINDS[tok], tok
            if kind == "angle":
                kind = "unitless"
        elif tok in DRU_UNIT_KINDS and not unitless:
            # KiCad reads a unit only as part of the number before it
            if not i:
                return None, None, (f"has unit {tok!r} with no number before "
                                    "it, which KiCad does not read")
            return None, None, (f"has unit {tok!r} after {before[1]!r}, which "
                                "KiCad reads only right after a number")
        else:
            follow = False
    if not follow:
        return None, None, None
    why = _dru_expr_error([t for t in toks if t[0] != "word"])
    if why:
        return None, None, why
    core = toks
    while core:
        if core[0] == ("op", "+"):
            core = core[1:]
        elif _dru_wrapped(core):
            core = core[1:-1]
        else:
            break
    if not core:
        return None, None, None
    if len(core) == 1 and core[0][0] == "num" and not unitless:
        return None, None, "has no unit"
    if kind == "time" and ctype not in DRU_TIME_TYPES:
        return None, None, (f"is a delay (its last unit is {unit!r}), which "
                            "KiCad takes only for length and skew")
    mm = None
    body = core[1:] if core[0] == ("op", "-") else core
    if (len(body) == 2 and body[0][0] == "num"
            and body[1][1] in DRU_LENGTH_UNITS):
        mm = float(body[0][1].replace(",", ".")) * DRU_LENGTH_UNITS[body[1][1]]
        mm = -mm if body is not core else mm
    return mm, kind, None


def dru_value(value: str | None, constraint: str) -> tuple:
    """One (min/opt/max ...) value of a constraint as KiCad 10 reads it ->
    (mm, None) for a plain length, 20mil read as 0.508; (None, None) for one
    it reads that is not a plain length (a count, an angle, a delay, a sum)
    or that this reader cannot follow; (None, why) for one it rejects, which
    voids the whole file (#1273). An old type name reads as the one it stands
    for; a type whose values are not read this way gives (None, None)."""
    ctype = DRU_TYPE_ALIASES.get(constraint, constraint)
    if ctype not in DRU_VALUE_TYPES:
        return None, None
    mm, _, why = _dru_scan(value, ctype)
    return mm, why


def dru_unit_errors(dru_text: str) -> list[str]:
    """Each value in a .kicad_dru that KiCad 10's rule parser rejects, as
    'rule "NAME": (TYPE (KEY VALUE)) why': a unit it does not read, or one
    not right after a number; a bare number where a unit is due; a unit on
    a count; a sum it cannot parse; a delay where only a length goes; a
    value of another kind than the one before it in its clause (a value is
    of the kind of its last unit, deg or none reading as unitless, so -1
    after 0.1mm is one). One is enough for KiCad to drop the whole file: DRC
    then holds none of its rules, kicad-cli says nothing, and the board
    falls back to its netclass, zone and board-setup values (#1273, #1309).
    [] for a file whose values KiCad reads. Other parse errors (a misspelled
    keyword, a bad condition, a type given twice) are not looked for
    here."""
    out = []
    for form in dru_forms(dru_text):
        rule = dru_rule_constraints(form)
        for ctype, vals in rule[1] if rule else ():
            if ctype not in DRU_VALUE_TYPES:
                continue
            last = None
            for key, value in vals:
                _, kind, why = _dru_scan(value, ctype)
                if not why and kind and last and kind != last:
                    why = (f"is {_DRU_KIND_NAMES[kind]} after "
                           f"{_DRU_KIND_NAMES[last]}, and KiCad takes one "
                           "kind per constraint (a value reads as its last "
                           "unit; deg or none is unitless)")
                if not why:
                    last = kind
                if why:
                    shown = value.translate({9: "\\t", 10: "\\n", 11: "\\v",
                                             12: "\\f", 13: "\\r"})
                    out.append(f'rule "{rule[0]}": ({ctype} ({key}'
                               f'{" " + shown if shown else ""})) {why}')
    return out


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


# ------------------------------------------------ DRU layer bans (#1140)

# The via kinds (route_finish's through via, hdilib.Span.kind) each item a
# `disallow` constraint names keeps out: `via` and `hole` are every via,
# each of KiCad 10's through_via, micro_via, blind_via and buried_via its
# own kind (drc_rule_parser.cpp: DRC_DISALLOW_BLIND_VIAS and _BURIED_VIAS
# are separate flags).
_ALL_VIAS = frozenset({"through", "micro", "blind", "buried"})
_VIA_KINDS = {"via": _ALL_VIAS, "hole": _ALL_VIAS,
              "through_via": frozenset({"through"}),
              "micro_via": frozenset({"micro"}),
              "blind_via": frozenset({"blind"}),
              "buried_via": frozenset({"buried"})}
# KiCad's canonical layer names besides the copper ones (LSET::Name), for a
# rule's `(layer ...)` that names a layer no route is on.
_KICAD_LAYERS = tuple(
    [f"In{i}.Cu" for i in range(1, 31)] + ["F.Cu", "B.Cu"]
    + [f"{s}.{k}" for k in ("Adhes", "Paste", "SilkS", "Mask", "CrtYd", "Fab")
       for s in "FB"]
    + ["Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User", "Edge.Cuts",
       "Margin", "Rescue"] + [f"User.{i}" for i in range(1, 46)])
_DNF_CAP = 256          # alternatives one condition may expand to, or
#                         its token count when that is more: an || chain
#                         grows with its text (rules_gen ORs every net of
#                         an impedance class), only && of ||s blows up
_COND_TOKEN = re.compile(
    r"\s*(?:('[^']*')|(&&|\|\||==|!=|!|\(|\))|([A-Za-z_][A-Za-z0-9_.]*))")


class _Unread(Exception):
    """A DRU condition this reader does not follow."""


def _kicad_match(pattern: str, text: str, case: bool = False) -> bool:
    """KiCad's string `==` in a rule: case-insensitive, `*` and `?` wild
    when the pattern has one (re, not fnmatch: a net name may hold [ ])."""
    if "*" not in pattern and "?" not in pattern:
        return pattern == text if case else pattern.lower() == text.lower()
    rx = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(rx, text, 0 if case else re.I) is not None


_COND_SLOT = {"A.NetName": 0, "A.Layer": 2, "A.NetClass": 4}


def _cond_dnf(cond: str) -> list[tuple]:
    """A rule condition as alternatives (any one matches), each a tuple
    (names, not_names, layers, not_layers, classes, not_classes) of
    patterns that must all hold: the item's net matches every name and no
    not_name, its layer every layer and no not_layer, its net class every
    class and no not_class. Only `A.NetName`, `A.Layer` and `A.NetClass`
    compared with == or != to a quoted string, && || and brackets are
    read; anything else raises _Unread."""
    toks: list[tuple] = []
    pos = 0
    while pos < len(cond):
        m = _COND_TOKEN.match(cond, pos)
        if not m or m.end() == pos:
            if cond[pos:].strip():
                raise _Unread(cond[pos:pos + 20])
            break
        pos = m.end()
        if m.group(1) is not None:
            toks.append(("str", m.group(1)[1:-1]))
        elif m.group(2) is not None:
            toks.append(("op", m.group(2)))
        elif m.group(3) is not None:
            toks.append(("id", m.group(3)))
    at = [0]
    cap = max(_DNF_CAP, len(toks))

    def peek():
        return toks[at[0]] if at[0] < len(toks) else (None, None)

    def take():
        tok = peek()
        at[0] += 1
        return tok

    def expr():
        alts = conj()
        while peek() == ("op", "||"):
            take()
            alts = alts + conj()
            if len(alts) > cap:
                raise _Unread("too many alternatives")
        return alts

    def conj():
        alts = atom()
        while peek() == ("op", "&&"):
            take()
            rhs = atom()
            alts = [tuple(a + b for a, b in zip(x, y))
                    for x in alts for y in rhs]
            if len(alts) > cap:
                raise _Unread("too many alternatives")
        return alts

    def atom():
        kind, val = take()
        if (kind, val) == ("op", "("):
            alts = expr()
            if take() != ("op", ")"):
                raise _Unread("unbalanced brackets")
            return alts
        if kind != "id" or val not in _COND_SLOT:
            raise _Unread(str(val))
        op = take()
        lit = take()
        if op not in (("op", "=="), ("op", "!=")) or lit[0] != "str":
            raise _Unread(f"{val} {op[1]} {lit[1]}")
        slot = _COND_SLOT[val] + (op[1] == "!=")
        return [tuple((lit[1],) if i == slot else () for i in range(6))]

    alts = expr() if toks else [((),) * 6]
    if at[0] != len(toks):
        raise _Unread(str(peek()[1]))
    return alts


def _sexp_bodies(text: str, head: str):
    """(name, body) of each top-level `(head "name" ...)` in an
    s-expression file, quote-aware; `#` comment lines dropped."""
    text = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    for m in re.finditer(r"\(\s*" + head + r'\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))',
                         text):
        name = m.group(1) if m.group(1) is not None else m.group(2)
        try:
            end = _sexp_end(text, m.start())
        except CheckError:
            raise CheckError(f"unbalanced s-expression at ({head} {name}"
                             ")") from None
        yield name, text[m.end():end - 1]


@dataclass
class DruBans:
    """What a .kicad_dru's `disallow` rules keep the router from (#1140).
    track: [(names, not_names, layers, classes, not_classes)]: a net whose
        name matches every name pattern (any net when there are none) and
        no not_name, and whose net class matches every class pattern and no
        not_class, may not have a track on any of `layers`;
    via: [(names, not_names, kinds, classes, not_classes)]: such a net may
        not have a via of those kinds (through, micro, blind, buried);
    net_classes: {net: (class, ...)}, the .kicad_pro's (pro_net_classes);
        a net not in it is in KiCad's Default class;
    unread: the disallow rules this can't follow (a B., Type or
        intersectsCourtyard term, `!`, a layer KiCad has no name for): DRC
        is left to judge them."""
    track: list = field(default_factory=list)
    via: list = field(default_factory=list)
    unread: list = field(default_factory=list)
    net_classes: dict = field(default_factory=dict)

    def _class_is(self, pattern: str, net: str) -> bool:
        """KiCad 10's `A.NetClass == pattern` (pcbexpr_evaluator.cpp,
        PCBEXPR_NETCLASS_VALUE): one of the net's classes, or their
        comma-joined name, matches (case-insensitive, wildcards); `!=` is
        true when neither does."""
        classes = tuple(self.net_classes.get(net) or ("Default",))
        return any(_kicad_match(pattern, c) for c in classes) \
            or _kicad_match(pattern, ",".join(classes))

    def _hits(self, names, not_names, classes, not_classes, net: str
              ) -> bool:
        return all(_kicad_match(p, net) for p in names) \
            and not any(_kicad_match(p, net) for p in not_names) \
            and all(self._class_is(p, net) for p in classes) \
            and not any(self._class_is(p, net) for p in not_classes)

    def track_layers(self, net: str) -> set:
        """The layers the net may not have a track on."""
        out: set = set()
        for names, not_names, layers, classes, not_classes in self.track:
            if self._hits(names, not_names, classes, not_classes, net):
                out |= layers
        return out

    def via_kinds(self, net: str) -> set:
        """The via kinds the net may not have."""
        out: set = set()
        for names, not_names, kinds, classes, not_classes in self.via:
            if self._hits(names, not_names, classes, not_classes, net):
                out |= kinds
        return out

    def via_banned(self, net: str) -> bool:
        """True when the net may have no through via."""
        return "through" in self.via_kinds(net)

    def facts(self) -> dict:
        """JSON for a report."""
        def one(n, x, c, nc):
            return {"nets": list(n), "not": list(x), "classes": list(c),
                    "not_classes": list(nc)}
        return {"track": [dict(one(n, x, c, nc), layers=sorted(ly))
                          for n, x, ly, c, nc in self.track],
                "via": [dict(one(n, x, c, nc), kinds=sorted(k))
                        for n, x, k, c, nc in self.via],
                "unread": list(self.unread)}


def pro_net_classes(pro: dict | None, nets) -> dict[str, tuple]:
    """{net: (class, ...)}: the net classes KiCad 10 gives each of `nets`
    from a .kicad_pro's net_settings (NET_SETTINGS::GetEffectiveNetClass):
    its netclass_assignments entry (a name or a list of them), then every
    netclass_patterns entry whose pattern matches the whole name, as a
    case-sensitive wildcard or, when it compiles, an anchored regex. A
    class the pro doesn't define is dropped, and a net with none is left
    out (KiCad's Default). Each net's classes go in priority order."""
    ns = (pro or {}).get("net_settings") or {}
    defined = {}
    for i, c in enumerate(ns.get("classes") or []):
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            pr = c.get("priority")
            defined[c["name"]] = pr if isinstance(pr, int) else 2 ** 31 - 1
    pats = []
    for e in ns.get("netclass_patterns") or []:
        pat = e.get("pattern") if isinstance(e, dict) else None
        cls = e.get("netclass") if isinstance(e, dict) else None
        if not isinstance(pat, str) or not pat or not isinstance(cls, str):
            continue
        rxs = [re.compile(re.escape(pat).replace(r"\*", ".*")
                          .replace(r"\?", "."), re.S)]
        try:
            rxs.append(re.compile(pat))
        except re.error:
            pass
        pats.append((rxs, cls))
    assigned = ns.get("netclass_assignments")
    assigned = assigned if isinstance(assigned, dict) else {}
    out: dict[str, tuple] = {}
    for net in nets:
        got = []
        a = assigned.get(net)
        got += [a] if isinstance(a, str) else \
            [x for x in a if isinstance(x, str)] if isinstance(a, list) else []
        got += [cls for rxs, cls in pats
                if any(r.fullmatch(net) for r in rxs)]
        got = [c for c in dict.fromkeys(got)
               if c in defined or c == "Default" or not defined]
        if got:
            out[net] = tuple(sorted(got, key=lambda c: (
                defined.get(c, 2 ** 31 - 1), c)))
    return out


def _layer_hit(pattern: str, layer: str, own: dict) -> bool:
    """KiCad's layer pattern (wxString::Matches: case-sensitive, `*` and
    `?` wild) against the layer's canonical name or the board's own."""
    return _kicad_match(pattern, layer, True) or (
        layer in own and _kicad_match(pattern, own[layer], True))


def dru_layer_bans(dru_text: str, signal_layers: list[str], *,
                   net_classes: dict | None = None,
                   layer_names: dict | None = None) -> DruBans:
    """The .kicad_dru's `disallow track` and `disallow via` rules (rules_gen
    writes aiee_diff_outer_only_*, aiee_z<Z>_outer_only*: an inner layer
    with no solved stripline geometry; aiee_sw_layer_*, aiee_sw_novia_*: a
    switch node on one layer, no vias), as DruBans. A rule's layer is a
    quoted or bare layer name (KiCad's wildcards, matched against the
    canonical name and the board's own, `layer_names` {canonical: own}),
    `inner` or `outer`; a rule with none is on every layer, one whose layer
    is a KiCad layer but no copper layer here on none, one whose layer
    names nothing KiCad knows unread (KiCad reports it as a rules error).
    A rule at `(severity ignore)` keeps nothing out (KiCad's disallow test
    skips it). A via ban on any layer is a ban of those via kinds anywhere
    (a through via crosses every layer; a span that would miss the layer
    is kept out too). signal_layers: the board's copper layer names, front
    first; net_classes: pro_net_classes'. CheckError on an unbalanced
    file."""
    signal = list(signal_layers)
    own = dict(layer_names or {})
    outer = {ly for ly in signal if ly in ("F.Cu", "B.Cu")}
    known = set(_KICAD_LAYERS) | set(own) | set(signal)
    bans = DruBans(net_classes=dict(net_classes or {}))
    for name, body in _sexp_bodies(dru_text or "", "rule"):
        if re.search(r"\(\s*severity\s+ignore\s*\)", body):
            continue
        kinds: set = set()
        for cm in re.finditer(r"\(\s*constraint\s+disallow\b([^()]*)\)", body):
            kinds |= set(cm.group(1).split())
        track = "track" in kinds
        via = frozenset().union(*(_VIA_KINDS.get(k, ()) for k in kinds))
        if not track and not via:
            continue
        layers = set(signal)
        lm = re.search(r'\(\s*layer\s+(?:"([^"]*)"|([^\s()]+))\s*\)', body)
        if lm:
            ly = lm.group(1) if lm.group(1) is not None else lm.group(2)
            if ly == "inner":
                layers = {x for x in signal if x not in outer}
            elif ly == "outer":
                layers = outer
            else:
                layers = {x for x in signal if _layer_hit(ly, x, own)}
                if not layers and not any(_layer_hit(ly, x, own)
                                          for x in known):
                    bans.unread.append(name)
                    continue
        if not layers:      # on no copper layer here: whatever it holds
            continue
        cm = re.search(r'\(\s*condition\s+"((?:[^"\\]|\\.)*)"\s*\)', body)
        try:
            alts = _cond_dnf(cm.group(1) if cm else "")
        except _Unread:
            bans.unread.append(name)
            continue
        for names, not_names, eq, ne, classes, not_classes in alts:
            on = {x for x in layers     # a layer compares case and all
                  if all(_layer_hit(p, x, own) for p in eq)
                  and not any(_layer_hit(p, x, own) for p in ne)}
            if not on:
                continue
            if track:
                bans.track.append((names, not_names, frozenset(on),
                                   classes, not_classes))
            if via:
                bans.via.append((names, not_names, via, classes,
                                 not_classes))
    return bans


def dru_track_bans(dru_text: str, signal_layers: list[str], *,
                   layer_names: dict | None = None) -> dict[str, set]:
    """The layers a .kicad_dru `disallow track` rule keeps each named net
    off, as {net: {layer, ...}}, for the DSN (dsn_apply_track_bans, #1108):
    dru_layer_bans' alternatives that name one net, no wildcard, nothing it
    must not be and no net class; any other condition is left to DRC.
    signal_layers: the board's copper layer names, front first."""
    bans: dict[str, set] = {}
    for names, not_names, layers, classes, not_classes in dru_layer_bans(
            dru_text, signal_layers, layer_names=layer_names).track:
        if len(names) == 1 and not not_names and not classes \
                and not not_classes \
                and not any(ch in names[0] for ch in "*?"):
            bans.setdefault(names[0], set()).update(layers)
    return bans


def dsn_apply_track_bans(dsn_text: str, bans: dict[str, set]
                         ) -> tuple[str, list]:
    """Keep each net's tracks off the layers `bans` names (dru_track_bans)
    by giving its class a Specctra `(circuit (use_layer ...))` of the
    signal layers it may use, which Freerouting reads as the class's active
    routing layers (#1108: the DRU kept the CK pair off In4, the DSN didn't,
    and Freerouting drew it there). A class whose nets are banned
    differently is split, the banned nets each to their own class (rules
    and vias kept). A net banned from every layer is left alone, for DRC to
    name. Returns (text, [{net, use_layer: the layers it may use}]); a DSN not in
    `(resolution um N)` comes back unchanged."""
    if not bans or not re.search(r"\(resolution\s+um\s+\d+\)", dsn_text):
        return dsn_text, []
    scan = _parser_blanked(dsn_text)
    span = _structure_span(scan)
    if span is None:
        return dsn_text, []
    signal = re.findall(r"\(layer\s+\"?([^\s\"()]+)\"?\s*\(type\s+(?:signal|mixed)\)",
                        scan[span[0]:span[1]])

    def allowed(net: str) -> tuple:
        ban = bans.get(net) or set()
        keep = tuple(ly for ly in signal if ly not in ban)
        return keep if ban & set(signal) and keep else ()

    def with_layers(body: str, keep: tuple) -> str:
        use = f"(use_layer {' '.join(keep)})"
        m = re.search(r"\(circuit\b", body)
        if m:
            end = _sexp_end(body, m.start())
            return body[:end - 1].rstrip() + f"\n        {use}\n      )" \
                + body[end:]
        return f"(circuit\n        {use}\n      )\n      " + body

    out, done, pos = [], [], 0
    for m in re.finditer(r"\(class\s", dsn_text):
        if m.start() < pos:
            continue
        end = _sexp_end(dsn_text, m.start())
        block = dsn_text[m.start():end]
        head_end = next((j for j, t in enumerate(_quote_mask(block))
                         if j and t == "("), -1)
        head = block[:head_end] if head_end > 0 else block[:-1]
        body = (block[head_end:-1] if head_end > 0 else "").strip()
        toks = _DSN_TOKEN_RE.findall(head)[1:]
        name, members = toks[0], toks[1:]
        out.append(dsn_text[pos:m.start()])
        pos = end
        hit = {t: allowed(t.strip('"')) for t in members}
        hit = {t: k for t, k in hit.items() if k}
        if not hit:
            out.append(block)
            continue
        keep = [t for t in members if t not in hit]
        alone = not keep and len(set(hit.values())) == 1
        parts = []
        if keep:
            parts.append(f"(class {name} {' '.join(keep)}\n      {body}\n    )")
        if alone:
            k = next(iter(hit.values()))
            parts.append(f"(class {name} {' '.join(members)}\n      "
                         f"{with_layers(body, k)}\n    )")
        else:
            for tok, k in hit.items():
                cname = "aiee_ban_" + re.sub(r"[^A-Za-z0-9_]", "_", tok.strip('"'))
                parts.append(f"(class {cname} {tok}\n      "
                             f"{with_layers(body, k)}\n    )")
        done += [{"net": t.strip('"'), "use_layer": list(k)} for t, k in hit.items()]
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


_DSN_UNIT_MM = {"um": 0.001, "mm": 1.0, "mil": 0.0254, "inch": 25.4}


def dsn_fixed_copper(dsn_text: str) -> dict:
    """The wires and vias a DSN gives Freerouting as `(type fix)`, in board
    mm (DSN y is up): {"segs": [(layer, (x1, y1), (x2, y2)), ...], "vias":
    [(x, y), ...]}. Freerouting sends no fix wire back in its SES, and
    KiCad's import deletes every unlocked track, so an unlocked segment
    KiCad's export folded into a locked chain's fix wire is lost (#664).
    Empty for a DSN this can't read."""
    out: dict = {"segs": [], "vias": []}
    scan = re.sub(r'\(string_quote\s+"\s*\)',
                  lambda q: q.group(0).replace('"', " "), dsn_text)
    mask = _quote_mask(scan)
    u = re.search(r"\(unit\s+(\w+)\s*\)", mask)
    k = _DSN_UNIT_MM.get(u.group(1) if u else "um")
    m = re.search(r"\(wiring[\s)]", mask)
    if k is None or not m:
        return out
    try:
        wend = _sexp_end(scan, m.start())
        for s, e in _child_spans(scan, m.start(), wend):
            body = _quote_mask(scan[s:e])
            if not re.search(r"\(type\s+fix\s*\)", body):
                continue
            head = _sexp_head(scan, s, e)
            if head == "wire":
                w = _parse_dsn_wire(scan, s, e)
                if not w:
                    continue
                pts = [(x * k, -y * k) for x, y in w["fpts"]]
                out["segs"] += [(w["layer"], a, b)
                                for a, b in zip(pts, pts[1:])]
            elif head == "via":
                cut = _quote_mask(scan[s + 1:e]).find("(")
                stop = s + 1 + cut if cut >= 0 else e - 1
                toks = _DSN_TOKEN_RE.findall(scan[s + 1:stop])
                try:
                    out["vias"].append((float(toks[2]) * k,
                                        -float(toks[3]) * k))
                except (IndexError, ValueError):
                    pass
    except CheckError:
        return {"segs": [], "vias": []}
    return out


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


def dsn_net_class_clearances(dsn_text: str) -> dict[str, float]:
    """{net: um} - the clearance each net's DSN class rule sets (a net in
    no class with its own clearance is left out)."""
    blank = _parser_blanked(dsn_text)
    scan = _quote_mask(blank)
    out: dict[str, float] = {}
    for m in re.finditer(r"\(class\s+", scan):
        end = _sexp_end(blank, m.start())
        body = scan[m.end():end - 1]
        c = re.search(r"\(clearance\s+([\d.]+)\s*\)", body)
        if not c:
            continue
        head = body.find("(")
        names = dsn_text[m.end():m.end() + (head if head >= 0 else len(body))]
        toks = [_dsn_unq(t) for t in _DSN_TOKEN_RE.findall(names)]
        for net in toks[1:]:
            out[net] = float(c.group(1))
    return out


def _dsn_unq(tok: str) -> str:
    return tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok


def _dsn_shrunk_shape(shape: str, d: float) -> str | None:
    """One padstack `(shape (...))` with its copper pulled in by d um on
    every side, or None for a form it can't shrink (polygon, too small)."""
    m = re.match(r"\(shape\s*\(\s*(circle|rect|path)\s+(\S+)\s+([^()]*)\)",
                 shape)
    if not m:
        return None
    kind, layer = m.group(1), m.group(2)
    try:
        nums = [float(v) for v in m.group(3).split()]
    except ValueError:
        return None
    if kind == "circle" and nums and nums[0] > 2 * d:
        nums[0] -= 2 * d
    elif kind == "rect" and len(nums) == 4:
        x1, y1, x2, y2 = (min(nums[0], nums[2]), min(nums[1], nums[3]),
                          max(nums[0], nums[2]), max(nums[1], nums[3]))
        if x2 - x1 <= 2 * d or y2 - y1 <= 2 * d:
            return None
        nums = [x1 + d, y1 + d, x2 - d, y2 - d]
    elif kind == "path" and nums and nums[0] > 2 * d:
        nums[0] -= 2 * d
    else:
        return None
    return (f"(shape ({kind} {layer} "
            f"{' '.join(f'{v:g}' for v in nums)}))")


def dsn_shrink_part_pads(dsn_text: str, shrink_um: dict[str, float],
                         keep_nets: set[str] | frozenset = frozenset()
                         ) -> tuple[str, dict]:
    """Give Freerouting a part's own, smaller pads where the board holds
    copper nearer to that part than the class clearance: a KiCad rule
    `(condition "A.intersectsCourtyard('U1') || ...")` with a lower
    clearance never reaches the DSN, so Freerouting keeps the class's
    clearance from every ball and finds no 0.2 mm track through a 0.45 mm
    BGA street (#1512). Each pad of the part is pulled in by d um a side,
    so the class clearance from the smaller pad is (class - d) from the real
    one, which the rule allows. The part's image is cloned under its own
    name (another part sharing the image keeps its pads) and its place
    moved to the clone. A pin on a net in keep_nets keeps its pad (a net
    holding more than the class wants it from everything). The SES import
    finds footprints by reference, so nothing is undone after routing.
    Returns (text, {ref: {"shrink_um", "image", "pads"}}); only a um DSN
    is handled."""
    if not shrink_um or not re.search(r"\(resolution\s+um\s+\d+\)",
                                       dsn_text) \
            or not re.search(r"\(unit\s+um\)", dsn_text):
        return dsn_text, {}
    blank = _parser_blanked(dsn_text)
    scan = _quote_mask(blank)
    tok = r'("[^"]*"|[^\s()]+)'

    pin_net: dict[str, str] = {}
    for m in re.finditer(r"\(net\s+" + tok, scan):
        end = _sexp_end(blank, m.start())
        net = _dsn_unq(dsn_text[m.start(1):m.end(1)])
        pm = re.search(r"\(pins\s+([^()]*)\)", scan[m.start():end])
        if not pm:
            continue
        body = dsn_text[m.start() + pm.start(1):m.start() + pm.end(1)]
        for p in _DSN_TOKEN_RE.findall(body):
            pin_net[_dsn_unq(p)] = net

    comps = []          # (start, end, image token, [(place start, end, ref)])
    for m in re.finditer(r"\(component\s+" + tok, scan):
        end = _sexp_end(blank, m.start())
        places = []
        for p in re.finditer(r"\(place\s+" + tok, scan[m.start():end]):
            ps = m.start() + p.start()
            places.append((ps, _sexp_end(blank, ps),
                           _dsn_unq(dsn_text[m.start() + p.start(1):
                                             m.start() + p.end(1)])))
        comps.append((m.start(), end, dsn_text[m.start(1):m.end(1)], places))

    images = {}
    for m in re.finditer(r"\(image\s+" + tok, scan):
        images[_dsn_unq(dsn_text[m.start(1):m.end(1)])] = \
            (m.start(), _sexp_end(blank, m.start()))
    stacks = {}
    for m in re.finditer(r"\(padstack\s+" + tok, scan):
        stacks[_dsn_unq(dsn_text[m.start(1):m.end(1)])] = \
            (m.start(), _sexp_end(blank, m.start()))

    edits: list[tuple[int, int, str]] = []
    new_stacks: dict[str, str] = {}
    facts: dict = {}
    for ref, d in sorted(shrink_um.items()):
        if d <= 0:
            continue
        hit = next(((c, pl) for c in comps for pl in c[3] if pl[2] == ref),
                    None)
        if hit is None:
            continue
        comp, place = hit
        img_name = _dsn_unq(comp[2])
        if img_name not in images:
            continue
        i0, i1 = images[img_name]
        block, bscan = dsn_text[i0:i1], scan[i0:i1]
        out, pos, pads = [], 0, 0
        for pm in re.finditer(r"\(pin\s+" + tok, bscan):
            pend = _sexp_end(blank, i0 + pm.start()) - i0
            rest = re.sub(r"\(rotate[^()]*\)", lambda r: " " * len(r[0]),
                          bscan[pm.end(1):pend - 1])
            idm = _DSN_TOKEN_RE.search(rest)
            if not idm:
                continue
            pin_id = _dsn_unq(block[pm.end(1) + idm.start():
                                    pm.end(1) + idm.end()])
            if pin_net.get(f"{ref}-{pin_id}") in keep_nets:
                continue
            stack = _dsn_unq(block[pm.start(1):pm.end(1)])
            if stack not in stacks:
                continue
            new = f"{stack}_cy{d:g}"
            if new not in new_stacks:
                s0, s1 = stacks[stack]
                sblock = dsn_text[s0:s1]
                shapes = [(s.start(), _sexp_end(_parser_blanked(sblock),
                                                s.start()))
                          for s in re.finditer(r"\(shape\b",
                                               _quote_mask(sblock))]
                shrunk = [_dsn_shrunk_shape(sblock[a:b], d)
                          for a, b in shapes]
                if not shapes or any(s is None for s in shrunk):
                    new_stacks[new] = ""
                else:
                    body = sblock
                    for (a, b), s in reversed(list(zip(shapes, shrunk))):
                        body = body[:a] + s + body[b:]
                    hm = re.match(r"\(padstack\s+" + tok, body)
                    new_stacks[new] = (f'\n    (padstack "{new}"'
                                       + body[hm.end():])
            if not new_stacks[new]:
                continue
            out.append(block[pos:pm.start(1)])
            out.append(f'"{new}"')
            pos = pm.end(1)
            pads += 1
        if not pads:
            continue
        out.append(block[pos:])
        clone = "".join(out)
        new_img = f"{img_name}_cy_{ref}"
        hm = re.match(r"\(image\s+" + tok, clone)
        clone = f'(image "{new_img}"' + clone[hm.end():]
        edits.append((i1, i1, "\n    " + clone))
        c0, c1, ctok, places = comp
        if len(places) == 1:
            m = re.match(r"\(component\s+" + tok, dsn_text[c0:c1])
            edits.append((c0 + m.start(1), c0 + m.end(1), f'"{new_img}"'))
        else:
            ptxt = dsn_text[place[0]:place[1]]
            ls = dsn_text.rfind("\n", 0, place[0])
            edits.append((ls if ls > c0 else place[0], place[1], ""))
            edits.append((c1, c1, f'\n    (component "{new_img}"\n      '
                                  f'{ptxt}\n    )'))
        facts[ref] = {"shrink_um": d, "image": new_img, "pads": pads}
    if not edits:
        return dsn_text, {}
    last = max(e for _, e in stacks.values()) if stacks else None
    if last is None:
        return dsn_text, {}
    add = "".join(t for t in new_stacks.values() if t)
    edits.append((last, last, add))
    out_text = dsn_text
    for a, b, t in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out_text = out_text[:a] + t + out_text[b:]
    return out_text, facts


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
    return _reorder_children(text, scan, head, child,
                             lambda order, _blocks: rng.shuffle(order))


def _reorder_children(text: str, scan: str, head: str, child: str,
                      reorder) -> str:
    """Reorder the `(child ...)` children of the first `(head ...)` section;
    every other child keeps its place. reorder(order, blocks) permutes the
    index list `order` in place (blocks: the children's text)."""
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
    reorder(order, blocks)
    out, pos = [], 0
    for (s, e), k in zip(spans, order):
        out.append(text[pos:s])
        out.append(blocks[k])
        pos = e
    out.append(text[pos:])
    return "".join(out)


_DSN_NET_NAME_RE = re.compile(r'\(net\s+("(?:[^"\\]|\\.)*"|[^\s()]+)')


def dsn_sort_nets(dsn_text: str) -> str:
    """The same design with its network nets in name order. KiCad numbers
    nets in the order it meets them as it loads the board, which follows
    the footprint order, and the DSN lists nets by number; Freerouting
    takes them in file order (see dsn_shuffle). With the export's
    sort_refs (route_swig) and dsn_sort_wiring this makes the DSN
    independent of the board's footprint order (#1412). Unparseable input
    comes back unchanged."""
    def by_name(order, blocks):
        def key(k):
            m = _DSN_NET_NAME_RE.match(blocks[k])
            return (m.group(1).strip('"') if m else "", k)
        order.sort(key=key)
    try:
        return _reorder_children(dsn_text, _parser_blanked(dsn_text),
                                 "network", "net", by_name)
    except CheckError:
        return dsn_text



def dsn_sort_wiring(dsn_text: str) -> str:
    """The same design with its pre-routed wiring in text order: wires
    among the wires' places and vias among the vias'. KiCad exports tracks
    in board order, which is the order a step added them (route_escape
    fans each BGA out in footprint order), and Freerouting reads them in
    file order (#1412). Unparseable input comes back unchanged."""
    def by_text(order, blocks):
        order.sort(key=lambda k: (blocks[k], k))
    try:
        out = dsn_text
        for child in ("wire", "via"):
            out = _reorder_children(out, _parser_blanked(out), "wiring",
                                    child, by_text)
        return out
    except CheckError:
        return dsn_text

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
