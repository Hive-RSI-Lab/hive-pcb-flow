"""hwde's planted faults: the share its own gates and checks still catch.

    python bin/strength.py        (the venv's python; bin/score.py calls it)

The score (bin/score.py) charges what hwde's gates find on pinned boards.
A check made looser finds less, so on its own that number could rise by
loosening hwde's checks. This is the other half: faults planted where each
gate's verdict comes from, run through the same code the gates run, offline
and in seconds. Each fault that gets through charges CRASH, as a gate that
can't run does: a check that misses a fault it was built to catch isn't
measuring the boards, so loosening one costs more than the findings it
could hide (docs/score.md, "The planted faults").

  gate    reference/gates.yaml through gate.evaluate: each of the gates the
          boards pass fails a report carrying one real finding of each
          kind it fails on. FAILS_ON pins those severities. Each finding is
          one a real run wrote, put among the findings of a report a real
          run wrote: erc and drc_routed's are KiCad's own, kept in
          tests/golden/gates/; verify's and dfm's are what their checks
          raise on the verify and dfm legs' planted faults, as they raise
          them now. Nothing in them says they were planted, so gate.py
          can't let them through without letting the same finding through
          on a board.
  verify  tests/golden's mutants, through tests/golden's scorecard
          (score_checks: each mutant run through the one check that owns
          it). Every check in the verify suite (verify_all.load_checks)
          must own at least one mutant, and a mutant whose check left the
          suite is a miss, so taking a check or its fault out costs too.
  dfm     the bench's P9 fixtures with a known answer (tests/fixtures/
          stages), and faults planted at JLC's published floors in a copy
          of pd-trigger's shipped drill file (DRILL_FAULTS): a hole closer
          to its neighbour than JLC's absolute 0.45 mm, a drill under its
          absolute 0.15 mm (under min_* but over abs_min_* is only a warning,
          #554). A floor in jlc_capabilities.yaml lowered past them lets the
          fault through.

ERC and DRC themselves are KiCad's; their planted faults would need
kicad-cli and are not here (the gate leg still holds what hwde decides
about their reports).

faults() gives (gate step, fault, caught) for each; run alone it prints one
line per fault and exits 1 when one got through.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS / "lib")]

# The severities each gate the score counts must fail on (SPEC P4: ERC
# "must be clean"; drc_routed fails on warnings since S3).
FAILS_ON = {"erc": ("error", "warning"), "drc_routed": ("error", "warning"),
            "verify": ("error",), "dfm": ("error",)}
# (name, kind dfm must report, fault): faults under JLC's absolute floors
# (pad hole edge to hole edge 0.45 mm, drill 0.15 mm), planted in pd-trigger's
# shipped drill file. "pair" is a hole beside an existing one, "lone" a
# hole of its own, each away from every other hole.
DRILL_FAULTS = (
    ("hole-to-hole-0.40", "dfm_hole_to_hole", {"pair": 0.40}),
    ("drill-0.12", "dfm_hole_size", {"lone": 0.12}),
)
DRILL_BOARD = "pd_trigger_dfm"
# (name, kind, vendor, capability row, gap mm): an inner-layer plane planted
# in the same gerber set with its antipad this far from a plated hole's wall,
# under the vendor's plated-drill-to-inner-copper floor (OSH Park 10 mil,
# #700). pd-trigger is 2 layers, so the plane is a new In1.Cu gerber and the
# row is the vendor's 4-layer one; lowering that floor past the gap lets it
# through.
INNER_FAULTS = (
    ("hole-to-inner-copper-0.15", "dfm_hole_to_inner_copper", "oshpark",
     "4layer_1oz", 0.15),
)
# Real findings for the gates whose tool is KiCad's (no kicad-cli offline):
# tests/golden/gates/<name>.json, a report kc.py wrote, holding the findings
# picked from it (README.md there says from which runs).
GATE_CORPUS = ROOT / "tests" / "golden" / "gates"
GATE_BASE = {"erc": "erc", "drc_routed": "drc"}
# The report a real run of verify and dfm wrote on a pinned board: the
# planted finding goes in among its own.
BASE_REPORT = {"verify": "pinned/lite-main/reports/checks/summary.json",
               "dfm": "pinned/lite-main/reports/dfm-check.json"}
_HIT = re.compile(r"^X(-?[\d.]+)Y(-?[\d.]+)$")
_TOOL = re.compile(r"^T(\d+)C([\d.]+)$")


def _plant_in(base: dict, v: dict) -> dict:
    """base, a report a real run of the gate's tool wrote, with v put among
    its findings and its counts (and verify's per-check status and coverage)
    as that run would have written them."""
    r = json.loads(json.dumps(base))
    vs = r.get("violations") or []
    at = int(hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest(), 16) % (len(vs) + 1)
    vs.insert(at, v)
    r["violations"] = vs
    r["status"] = "violations"
    by_sev: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for f in vs:
        by_sev[f.get("severity")] = by_sev.get(f.get("severity"), 0) + 1
        by_src[f.get("source")] = by_src.get(f.get("source"), 0) + 1
    counts = r.setdefault("counts", {})
    counts.update(total=len(vs), by_severity=by_sev, by_source=by_src)
    src = v.get("source")
    if "by_check" in counts:
        counts["by_check"][src] = by_src[src]
    if isinstance(r.get("checks"), dict) and src in r["checks"]:
        c = r["checks"][src]
        c["status"] = "violations"
        mine = [f for f in vs if f.get("source") == src]
        c["counts"] = {"total": len(mine),
                       "by_severity": {s: sum(f.get("severity") == s for f in mine)
                                       for s in {f.get("severity") for f in mine}},
                       "by_source": {src: len(mine)}}
    cov = r.get("coverage")
    if isinstance(cov, dict) and v.get("severity") == "error":
        cov["passed"] = [n for n in cov.get("passed") or [] if n != src]
        cov["failed"] = sorted(set(cov.get("failed") or []) | {src})
        cov["ran"] = sorted(set(cov.get("ran") or []) | {src})
    return r


def _corpus(name: str) -> dict:
    return json.loads((GATE_CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def gate_faults(found: dict[str, list[dict]] | None = None) -> list[tuple[str, str, bool]]:
    """Each gate the score counts, given a real finding of each kind it
    fails on, put in a report a real run wrote, must fail. erc and
    drc_routed take theirs from tests/golden/gates/ (kicad-cli's own
    findings, kept with where each came from); verify and dfm take the
    findings their checks raise on the planted faults of the other legs
    (`found`, by gate), so each is what the check writes today. Nothing in
    a planted report says it was planted: gate.py can't skip them without
    skipping the same finding on a real board."""
    import gate
    gates = gate.load_gates(gate.DEFAULT_GATES)
    found = found or {}
    out = []
    for name, sevs in FAILS_ON.items():
        g = gates.get(name)
        if name in GATE_BASE:
            base = _corpus(GATE_BASE[name])
            pool = base.pop("violations")
            base["violations"] = []
        else:
            base = json.loads((ROOT / BASE_REPORT[name]).read_text(encoding="utf-8"))
            pool = found.get(name) or []
        seen = set()
        for v in pool:
            sev = v.get("severity")
            key = (v.get("source"), v.get("check"), v.get("kind"), sev)
            if sev not in sevs or key in seen:
                continue
            seen.add(key)
            caught = g is not None and \
                gate.evaluate(name, g, _plant_in(base, v))["status"] == "fail"
            what = v.get("kind") or v.get("check")
            out.append((name, f"gate {name}: {v.get('source')} {what} ({sev})", caught))
        for sev in sevs:
            if not any(v.get("severity") == sev for v in pool):
                out.append((name, f"gate {name}: no {sev} to plant", False))
    return out


def verify_faults(found: dict | None = None) -> list[tuple[str, str, bool]]:
    import score_checks
    import verify_all
    checks = verify_all.load_checks()
    names = [c["name"] for c in checks]
    manifest = score_checks.load_manifest()
    out = []
    for m, rec in manifest["mutants"].items():
        if rec["check"] not in names and rec["check"] != "dfm_check":
            out.append(("verify", f"verify {m}: its check {rec['check']} is not in the suite", False))
    jobs = score_checks.mutant_jobs(manifest, names)
    hits: list = []
    res = score_checks.score(jobs, [], 4, checks, hits=hits)
    if found is not None:
        found.setdefault("verify", []).extend(v for _, _, v in hits)
    missed = {(f["check"], f["item"]) for f in res["findings"]
              if f.get("verdict") in ("miss", "error")}
    for j in jobs:
        out.append(("verify", f"verify {j['item']} ({j['target']})",
                    (j["target"], j["item"]) not in missed))
    owned = {j["target"] for j in jobs}
    for n in names:
        if n not in owned:
            out.append(("verify", f"verify {n}: no planted fault", False))
    return out


def _dfm(fx: dict, gerbers: Path) -> list[dict]:
    import dfm_check
    files = {k: ROOT / r["path"] for k, r in fx["files"].items()}
    args = fx.get("args") or {}
    return dfm_check.run(files["pcb"], fab_dir=gerbers,
                         copper_oz=float(args.get("copper_oz", 1.0)),
                         netlist=files.get("netlist"))["violations"]


def _plant(drl: str, fault: dict) -> str:
    """drl with one planted hole: a new tool, and a hit placed so its gap to
    every hole but the one it is planted beside is over 2 mm."""
    lines = drl.splitlines()
    tools, hits, tool = {}, [], None
    for ln in lines:
        if (t := _TOOL.match(ln)):
            tools[t.group(1)] = float(t.group(2))
        elif ln.startswith("T") and ln[1:].isdigit():
            tool = ln.strip()[1:]
        elif (h := _HIT.match(ln)) and tool in tools:
            hits.append((float(h.group(1)), float(h.group(2)), tools[tool]))
    if not hits:
        raise ValueError("drill file has no hits")

    def free(x, y, d, but=None):
        return all(((x - hx) ** 2 + (y - hy) ** 2) ** .5 - (d + hd) / 2 > 2.0
                   for i, (hx, hy, hd) in enumerate(hits) if i != but)
    xs, ys = [h[0] for h in hits], [h[1] for h in hits]
    spot = None
    if "pair" in fault:
        for i, (hx, hy, hd) in enumerate(hits):
            r = hd + fault["pair"]           # same drill: centre to centre
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                x, y = hx + dx * r, hy + dy * r
                if free(x, y, hd, but=i):
                    spot, dia = (x, y), hd
                    break
            if spot:
                break
    else:
        dia = fault["lone"]
        n = 40
        for a in range(1, n):
            for b in range(1, n):
                x = min(xs) + (max(xs) - min(xs)) * a / n
                y = min(ys) + (max(ys) - min(ys)) * b / n
                if free(x, y, dia):
                    spot = (x, y)
                    break
            if spot:
                break
    if spot is None:
        raise ValueError(f"no room to plant {fault}")
    t = str(max(int(k) for k in tools) + 1)
    head = lines.index("%")
    end = max(i for i, ln in enumerate(lines) if ln.strip() == "M30")
    body = [f"T{t}", f"X{spot[0]:.3f}Y{spot[1]:.3f}"]
    lines = lines[:head] + [f"T{t}C{dia:.3f}"] + lines[head:end] + body + lines[end:]
    return "\n".join(lines) + "\n"


def _inner_plane(fab, gap: float) -> tuple[str, tuple[float, float]]:
    """An In1.Cu gerber: a 2 mm square plane frame around the first plated
    hole with no other hole within 3 mm, its square antipad `gap` from the
    hole's wall. -> (gerber text, the hole's centre)."""
    holes = [h for h in fab.holes if h.plated]
    for h in holes:
        if all(((h.x - o.x) ** 2 + (h.y - o.y) ** 2) ** .5 > 3.0
               for o in fab.holes if o is not h):
            break
    else:
        raise ValueError("no lone plated hole to plant beside")
    a, b = h.diameter / 2.0 + gap, h.diameter / 2.0 + gap + 1.0

    def um(v: float) -> int:
        return round(v * 1e6)
    rects = ((-b, -b, b, -a), (-b, a, b, b), (-b, -a, -a, a), (a, -a, b, a))
    out = ["%TF.FileFunction,Copper,L2,Inr*%", "%FSLAX46Y46*%", "%MOMM*%",
           "%LPD*%", "G01*"]
    for x0, y0, x1, y1 in rects:
        # gerber Y is up, the board's (and fab.holes') Y is down
        x0, x1, y0, y1 = h.x + x0, h.x + x1, -h.y + y0, -h.y + y1
        out += ["G36*", f"X{um(x0)}Y{um(y0)}D02*", f"X{um(x1)}Y{um(y0)}D01*",
                f"X{um(x1)}Y{um(y1)}D01*", f"X{um(x0)}Y{um(y1)}D01*",
                f"X{um(x0)}Y{um(y0)}D01*", "G37*"]
    return "\n".join(out + ["M02*"]) + "\n", (h.x, h.y)


def inner_faults(src: Path, tmp: Path, found: dict | None = None
                 ) -> list[tuple[str, str, bool]]:
    """INNER_FAULTS, each held by dfm_check's hole-to-inner-copper measure
    on the vendor's own capability row."""
    import dfm_check
    import gerblib
    import vendors
    out = []
    for name, kind, vendor, row, gap in INNER_FAULTS:
        rules = vendors.load(vendor)["design_rules"][row]
        work = tmp / name
        shutil.copytree(src, work)
        stem = next(work.glob("*-F_Cu.*")).name.split("-F_Cu.")[0]
        text, at = _inner_plane(gerblib.FabStack(work), gap)
        (work / f"{stem}-In1_Cu.gbr").write_text(text, encoding="utf-8")
        vios: list = []
        dfm_check.check_hole_to_inner(gerblib.FabStack(work), rules, vios)
        new = [v for v in vios if v.get("kind") == kind
               and v.get("severity") == "error"
               and v.get("pos") and abs(v["pos"][0] - at[0]) < 0.01
               and abs(v["pos"][1] - at[1]) < 0.01]
        if found is not None:
            found.setdefault("dfm", []).extend(new)
        out.append(("dfm", f"dfm {name} ({kind}, {vendor} {row})", bool(new)))
    return out


def dfm_faults(found: dict | None = None) -> list[tuple[str, str, bool]]:
    import benchlib
    import yaml
    stages = ROOT / "tests" / "fixtures" / "stages"
    fixtures = yaml.safe_load((stages / "manifest.yaml").read_text(encoding="utf-8"))["fixtures"]
    out = []

    def mine(vs, kind):
        return [v for v in vs if v.get("kind") == kind and v.get("severity") == "error"]
    # dfm runs on a copy of each fixture's gerbers: a run may keep what it
    # learns (a fetched model, EasyEDA's "no model") beside the CPL, and a
    # pinned fixture must stay as frozen
    with tempfile.TemporaryDirectory(prefix="hwde-strength-") as tmp:
        for fid, fx in fixtures.items():
            if fx.get("stage") != "P9" or not fx.get("known_answer"):
                continue
            work = Path(tmp) / "fixtures" / fid
            shutil.copytree(ROOT / fx["dirs"]["gerbers"]["path"], work)
            vs = _dfm(fx, work)
            ka = benchlib.match_known_answer(fx["known_answer"], vs)
            out.append(("dfm", f"dfm {fid}", ka["status"] == "ok"))
        fx = fixtures[DRILL_BOARD]
        src = Path(tmp) / "fixtures" / "drill_clean"
        shutil.copytree(ROOT / fx["dirs"]["gerbers"]["path"], src)
        clean = _dfm(fx, src)
        for name, kind, fault in DRILL_FAULTS:
            work = Path(tmp) / name
            shutil.copytree(src, work)
            drl = next(work.glob("*.drl"))
            drl.write_text(_plant(drl.read_text(encoding="utf-8"), fault), encoding="utf-8")
            new = [v for v in mine(_dfm(fx, work), kind) if v not in mine(clean, kind)]
            if found is not None:
                found.setdefault("dfm", []).extend(new)
            out.append(("dfm", f"dfm {name} ({kind})", bool(new)))
        out += inner_faults(src, Path(tmp), found)
    return out


def faults() -> list[tuple[str, str, bool]]:
    """(gate step, fault, caught) for every planted fault. A leg that can't
    run raises: the score then can't be read, rather than read as caught."""
    found: dict[str, list[dict]] = {}
    v, d = verify_faults(found), dfm_faults(found)
    return gate_faults(found) + v + d


if __name__ == "__main__":
    fs = faults()
    for step, name, ok in fs:
        print(f"{'caught' if ok else 'MISSED'}  {step:10} {name}")
    print(f"planted faults caught: {sum(ok for *_, ok in fs)}/{len(fs)}")
    sys.exit(0 if all(ok for *_, ok in fs) else 1)
