#!/usr/bin/env python
"""errata_corpus.py - does hwde catch on rev A what the designer fixed in rev B?

The manifest is reference/errata_corpus.yaml: public boards whose next
revision fixed a defect the first one shipped with, each defect quoted from
the designer's errata. A defect with a `catch` names the hwde check and the
finding that is it; this script runs that check on rev A, where the finding
must be, and on rev B, where it must be gone (a check that also fires on the
fixed board isn't seeing the defect). The table goes to docs/errata-corpus.md.

    errata_corpus.py fetch [ID...]   sparse clones at the pinned commit
    errata_corpus.py run   [ID...]   run each catch on rev A and rev B
    errata_corpus.py table           write docs/errata-corpus.md

--cache DIR (default ~/.cache/hwde-errata-corpus, or HWDE_ERRATA_CACHE). Nothing
is fetched into this repo. Checks run here (RUNNERS), each with an empty
constraints file, what hwde reads before anyone has written one:
netlist_audit on the schematic, check_mate_pins and check_current on the
board, check_straps on the board with the netlist kicad-cli exports from the
schematic, check_connector_fit and check_silk on the board (no parts/),
check_pinouts on the schematic, check_bom_sync on the board with --live (the package of each LCSC part
comes from JLC's record, as nothing in the foreign tree holds it), check_thermal on the board
(the power ICs thermal_parts.yaml knows), and dfm's shipped-CPL check on the
CPL a revision published (its `cpl:`, the file the designer sent the
assembler), with that CPL's LCSC models fetched live into the cache beside a
copy of it. A catch for another script is reported as not run.

A catch may give `constraints: {rev_a: {...}, rev_b: {...}}`: what that
revision's constraints.json would say, written in place of the empty file
(a defect only a stated fact can show, such as the mate a cable goes to; its
verdict is caught_with_constraints).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MANIFEST = ROOT / "reference" / "errata_corpus.yaml"
DOC = ROOT / "docs" / "errata-corpus.md"
VERDICTS = {"caught", "caught_with_constraints", "missed", "not_static"}


def cache_root(arg: str | None) -> Path:
    c = Path(arg or os.environ.get("HWDE_ERRATA_CACHE")
             or Path.home() / ".cache" / "hwde-errata-corpus").resolve()
    if c == ROOT or ROOT in c.parents:
        raise SystemExit(f"cache {c} is inside the repo")
    return c


def load(path: Path = MANIFEST) -> list[dict]:
    boards = yaml.safe_load(path.read_text(encoding="utf-8"))["boards"]
    for b in boards:
        for d in b["defects"]:
            if d.get("verdict") not in VERDICTS:
                raise SystemExit(f"{d['id']}: verdict not one of {sorted(VERDICTS)}")
            if d["verdict"] == "caught" and not d.get("catch"):
                raise SystemExit(f"{d['id']}: caught with no catch")
            if d["verdict"] == "missed" and not d.get("follow_up"):
                raise SystemExit(f"{d['id']}: a miss needs a follow_up")
    return boards


def repo_dir(cache: Path, b: dict) -> Path:
    return cache / "repos" / f"{b['id']}@{b['commit'][:12]}"


def fetch(cache: Path, b: dict) -> None:
    dest = repo_dir(cache, b)
    dirs = sorted({str(Path(b[r][k]).parent) for r in ("rev_a", "rev_b")
                   for k in ("sch", "cpl") if b[r].get(k)})
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
                        b["url"], str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", *dirs], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", b["commit"]], check=True)


def matches(violation: dict, catch: dict) -> bool:
    """The finding is the defect: same kind, and the net or one of the refs."""
    if violation.get("kind") != catch["kind"]:
        return False
    if catch.get("net") and violation.get("net") != catch["net"]:
        return False
    refs = set(catch.get("refs") or ())
    return not refs or bool(refs & set(violation.get("refs") or ()))


def _netlist_audit(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["netlist_audit.py", "--sch", str(repo / b[rev]["sch"]),
            "--constraints", str(empty)]


def _check_mate_pins(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_mate_pins.py", "--pcb", str(repo / b[rev]["pcb"]),
            "--constraints", str(empty)]


def _check_current(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_current.py", "--pcb", str(repo / b[rev]["pcb"]),
            "--constraints", str(empty)]


def _check_pinouts(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_pinouts.py", "--sch", str(repo / b[rev]["sch"]),
            "--constraints", str(empty)]


def _check_connector_fit(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_connector_fit.py", "--pcb", str(repo / b[rev]["pcb"])]


def _check_silk(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_silk.py", "--pcb", str(repo / b[rev]["pcb"])]


def _check_bom_sync(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_bom_sync.py", "--pcb", str(repo / b[rev]["pcb"]), "--live"]


def _check_thermal(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board alone: with no thermal entry it screens the power ICs
    reference/thermal_parts.yaml knows (#884)."""
    return ["check_thermal.py", "--pcb", str(repo / b[rev]["pcb"]),
            "--constraints", str(empty)]


def _check_straps(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board, with the netlist kicad-cli exports from its schematic
    (what the pipeline puts beside an hwde board)."""
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import env
    cli = env.find_kicad_cli()
    if not cli:
        raise SystemExit("check_straps catches need kicad-cli (bin/hwde env)")
    net = empty.parent / f"{rev}.net"
    import legacy_net
    sch = repo / b[rev]["sch"]
    if legacy_net.is_legacy(sch):  # KiCad 4/5: kicad-cli exports it empty
        r = legacy_net.netlist(sch, net, pcb=repo / b[rev]["pcb"])
        if r["status"] != "pass":
            raise SystemExit(f"{b['id']} {rev}: {r['error']}")
    else:
        subprocess.run([str(cli), "sch", "export", "netlist", "-o", str(net),
                        str(sch)], capture_output=True, text=True,
                       timeout=900, check=True)
    return ["check_straps.py", "--pcb", str(repo / b[rev]["pcb"]),
            "--netlist", str(net)]


def _dfm(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """dfm's shipped-CPL check on the CPL this revision published (`cpl:`).
    dfm judges a foreign CPL only with its models kept beside it (else its
    verdicts would hang on whatever this machine has cached), so the CPL is
    copied into the run dir with an easyeda/ beside it that the check fills
    live and later runs reuse. Only the placement group runs: the catch is a
    CPL row, and the board's own copper rules are other checks' to judge."""
    import shutil
    src = b[rev].get("cpl")
    if not src:
        raise SystemExit(f"{b['id']} {rev}: a dfm catch needs the revision's cpl:")
    stage = empty.parent / f"{rev}-cpl"
    (stage / "easyeda").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / src, stage / "CPL.csv")
    return ["dfm_check.py", "--pcb", str(repo / b[rev]["pcb"]),
            "--cpl", str(stage / "CPL.csv"), "--no-polarity",
            "--skip", "copper,drill,silk,castellation,zone_fill,netlist"]


# script -> its argv on one revision (empty constraints: what hwde reads
# before anyone has written one)
RUNNERS = {"netlist_audit": _netlist_audit, "check_mate_pins": _check_mate_pins,
           "check_current": _check_current, "check_straps": _check_straps,
           "check_bom_sync": _check_bom_sync,
           "check_connector_fit": _check_connector_fit,
           "check_silk": _check_silk, "check_thermal": _check_thermal,
           "check_pinouts": _check_pinouts, "dfm": _dfm}


def audit(cache: Path, b: dict, rev: str, script: str,
          constraints: dict | None = None) -> list[dict]:
    """`script` on one revision, with `constraints` as its constraints.json
    (none: an empty one)."""
    tag = ""
    if constraints:
        import hashlib
        tag = "-c" + hashlib.sha1(json.dumps(constraints, sort_keys=True)
                                  .encode()).hexdigest()[:8]
    out = cache / "runs" / b["id"] / f"{rev}-{script}{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    empty = out.parent / f"constraints{tag}.json"
    empty.write_text(json.dumps(constraints or {}), encoding="utf-8")
    cmd, *args = RUNNERS[script](b, rev, repo_dir(cache, b), empty)
    subprocess.run([sys.executable, str(SCRIPTS / cmd), *args, "--out", str(out)],
                   capture_output=True, text=True, timeout=900)
    if not out.is_file():
        raise SystemExit(f"{b['id']} {rev}: {script} wrote no report")
    return json.loads(out.read_text(encoding="utf-8")).get("violations", [])


def run(cache: Path, b: dict) -> dict:
    found: dict[tuple[str, str, str], list[dict]] = {}
    res = {}
    for d in b["defects"]:
        c = d.get("catch")
        if not c:
            continue
        if c["script"] not in RUNNERS:
            res[d["id"]] = {"ran": False}
            continue
        hit = {}
        for rev in ("rev_a", "rev_b"):
            cons = (c.get("constraints") or {}).get(rev) or {}
            key = (rev, c["script"], json.dumps(cons, sort_keys=True))
            if key not in found:
                found[key] = audit(cache, b, rev, c["script"], cons)
            hit[rev] = any(matches(v, c) for v in found[key])
        res[d["id"]] = {"ran": True, **hit}
    (cache / "runs" / b["id"]).mkdir(parents=True, exist_ok=True)
    (cache / "runs" / b["id"] / "result.json").write_text(json.dumps(res, indent=1))
    return res


def outcome(d: dict, r: dict | None) -> str:
    if not d.get("catch"):
        return d["verdict"]
    if not r or not r.get("ran"):
        return f"{d['verdict']} (not run here)"
    if r["rev_a"] and not r["rev_b"]:
        return "caught on rev A, gone on rev B"
    if r["rev_a"]:
        return "REGRESSED: fires on rev B too"
    return "REGRESSED: not found on rev A"


def table(cache: Path, boards: list[dict]) -> str:
    lines = ["# The real-errata corpus", "",
             "Public boards whose next revision fixed a defect the first one shipped with,",
             "quoted from the designer's own errata (`reference/errata_corpus.yaml`), and",
             "what pcb-flow's checks do with the first revision. Written by",
             "`bin/pcb-flow errata_corpus table` after `fetch` and `run`.", ""]
    count: dict[str, int] = {}
    for b in boards:
        rf = cache / "runs" / b["id"] / "result.json"
        res = json.loads(rf.read_text()) if rf.exists() else {}
        lines += [f"## {b['id']}: {b['rev_a']['name']} -> {b['rev_b']['name']}", "",
                  f"[{b['url']}]({b['url']}) at `{b['commit'][:12]}`, {b['licence']}; "
                  f"errata `{b['source']}`.", "",
                  "| defect | class | pcb-flow on rev A | follow-up |", "|---|---|---|---|"]
        for d in b["defects"]:
            count[d["verdict"]] = count.get(d["verdict"], 0) + 1
            catch = d.get("catch")
            how = outcome(d, res.get(d["id"]))
            if catch:
                how += f" (`{catch['script']}` {catch['kind']}" + (
                    ", with constraints" if catch.get("constraints") else "") + ")"
            lines.append(f"| {d['id']}: {d['summary']} | {d['fault_class']} | {how} | "
                         f"{d.get('follow_up') or d.get('note') or ''} |".replace("\n", " "))
        lines.append("")
    total = sum(count.values())
    lines += ["## Count", "", f"{total} defects: " + ", ".join(
        f"{v} {k}" for k, v in sorted(count.items())) + ".", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["fetch", "run", "table"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--cache")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(DOC))
    a = ap.parse_args(argv)
    cache = cache_root(a.cache)
    boards = [b for b in load(Path(a.manifest)) if not a.ids or b["id"] in a.ids]
    bad = 0
    for b in boards if a.cmd != "table" else []:
        if a.cmd == "fetch":
            fetch(cache, b)
        else:
            for did, r in run(cache, b).items():
                print(did, json.dumps(r))
                bad += r.get("ran", False) and not (r["rev_a"] and not r["rev_b"])
    if a.cmd == "table":
        Path(a.out).write_text(table(cache, boards), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
