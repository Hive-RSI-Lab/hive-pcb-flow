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
    errata_corpus.py sweep ID...     every check on rev A and rev B, blind
    errata_corpus.py table           write docs/errata-corpus.md

Two sets. `boards:` is the training set: most of its catches were written
from the defects they catch, so its count is how well the checks fit what
they were built from. `holdout:` is boards no rule was written from: each is
swept (every check, both revisions) with the flow as it stood, before anyone
reads its findings against its errata, and each defect keeps that first
judgement as `blind:` for good. A rule written later may still catch a
holdout defect (its `verdict` moves, the rule named in `caught_by`), but only
`blind` says how the flow does on a board it has never seen; and a rule
seeded from a holdout defect moves that board to `boards:`.

--cache DIR (default ~/.cache/hwde-errata-corpus, or HWDE_ERRATA_CACHE). Nothing
is fetched into this repo. Checks run here (RUNNERS), each with an empty
constraints file, what hwde reads before anyone has written one:
netlist_audit on the schematic, check_mate_pins and check_current on the
board, check_straps on the board with the netlist kicad-cli exports from the
schematic, check_connector_fit, check_silk and check_esd on the board (no
parts/, so check_esd reads no victim's abs max), check_pinouts on the
schematic, check_bom_sync on the board with --live (the package of each LCSC part
comes from JLC's record, as nothing in the foreign tree holds it),
check_fp_package on the board with --live (each part's package from JLC's
record of its LCSC/JLC number or of the exact part its MPN names), check_thermal on the board
(the power ICs thermal_parts.yaml knows), and dfm's shipped-CPL check on the
CPL a revision published (its `cpl:`, the file the designer sent the
assembler), with that CPL's LCSC models fetched live into the cache beside a
copy of it. A catch for another script is reported as not run. A board
older than KiCad 8 (KiCad 5's `module` footprints, which geom doesn't read,
or KiCad 6/7's `fp_text reference`, which placelib reads no reference from,
#1123) is first brought to the current format by `kicad-cli pcb upgrade` on a copy in the run
dir, as intake does to every board; the board checks read that copy.

A catch may give `constraints: {rev_a: {...}, rev_b: {...}}`: what that
revision's constraints.json would say, written in place of the empty file
(a defect only a stated fact can show, such as the mate a cable goes to; its
verdict is caught_with_constraints).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MANIFEST = ROOT / "reference" / "errata_corpus.yaml"
DOC = ROOT / "docs" / "errata-corpus.md"
VERDICTS = {"caught", "caught_with_constraints", "missed", "not_static"}
BLIND = {"caught", "missed", "not_static"}


def cache_root(arg: str | None) -> Path:
    c = Path(arg or os.environ.get("HWDE_ERRATA_CACHE")
             or Path.home() / ".cache" / "hwde-errata-corpus").resolve()
    if c == ROOT or ROOT in c.parents:
        raise SystemExit(f"cache {c} is inside the repo")
    return c


def sealed_files(path: Path = MANIFEST) -> dict[int, Path]:
    """Sealed holdout rounds: `holdout/round-N.yaml` at the repo's top.

    The folder sits outside `reference/` and `docs/`, so a grep there for a
    part or a check doesn't reach a sealed defect.

    A round stays in its own file while its defects are unread by rule
    builders; builders open nothing under `holdout/` (see "A new holdout
    round"). Unsealing a round moves its boards into the manifest."""
    out = {}
    for f in sorted((path.parent.parent / "holdout").glob("round-*.yaml")):
        m = re.fullmatch(r"round-(\d+)\.yaml", f.name)
        if m:
            out[int(m.group(1))] = f
    return out


def _raw(path: Path, section: str) -> list[dict]:
    """A section of the manifest, plus every sealed round for `holdout`."""
    boards = list(yaml.safe_load(path.read_text(encoding="utf-8")).get(section) or [])
    if section != "holdout":
        return boards
    for n, f in sealed_files(path).items():
        sealed = yaml.safe_load(f.read_text(encoding="utf-8")).get("holdout") or []
        for b in sealed:
            r = (b.get("swept") or {}).get("round")
            if r != n:
                raise SystemExit(f"{b.get('id')}: in {f.name} but swept round {r}")
        boards += sealed
    return boards


def load(path: Path = MANIFEST, section: str = "boards") -> list[dict]:
    """One set of the manifest: `boards` (training) or `holdout`, the
    holdout including the sealed rounds under `holdout/`."""
    boards = _raw(path, section)
    for b in boards:
        for r in ("rev_a", "rev_b"):
            if r in b and "commit" in b[r] and len(str(b[r]["commit"])) != 40:
                raise SystemExit(f"{b['id']} {r}: commit must be a full sha")
        if section == "holdout":
            sw = b.get("swept") or {}
            if len(str(sw.get("flow", ""))) != 40 or not sw.get("date") \
                    or not isinstance(sw.get("round"), int) or sw["round"] < 1:
                raise SystemExit(f"{b['id']}: a holdout board needs swept: "
                                 f"{{flow: SHA, date, round: N}}")
        for d in b["defects"]:
            if d.get("verdict") not in VERDICTS:
                raise SystemExit(f"{d['id']}: verdict not one of {sorted(VERDICTS)}")
            if d["verdict"] == "caught" and not d.get("catch"):
                raise SystemExit(f"{d['id']}: caught with no catch")
            if d["verdict"] == "missed" and not d.get("follow_up"):
                raise SystemExit(f"{d['id']}: a miss needs a follow_up")
            if section == "holdout":
                if d.get("blind") not in BLIND:
                    raise SystemExit(f"{d['id']}: blind not one of {sorted(BLIND)}")
                if d["verdict"] == "caught" and d["blind"] != "caught" \
                        and not d.get("caught_by"):
                    raise SystemExit(f"{d['id']}: caught after the sweep needs caught_by")
    return boards


def load_all(path: Path = MANIFEST) -> list[dict]:
    return load(path) + load(path, "holdout")


def repo_dir(cache: Path, b: dict, rev: str | None = None) -> Path:
    """The checkout a revision's files are read from. A board whose
    revisions sit side by side is one checkout at `commit`; a revision with a
    `commit` of its own (a repo that kept its revisions only in history) gets
    its own checkout."""
    sha = (b.get(rev) or {}).get("commit") or b["commit"] if rev else b["commit"]
    return cache / "repos" / f"{b['id']}@{sha[:12]}"


def fetch(cache: Path, b: dict) -> None:
    by_dest: dict[Path, tuple[str, set]] = {}
    for r in ("rev_a", "rev_b"):
        dest = repo_dir(cache, b, r)
        sha = b[r].get("commit") or b["commit"]
        by_dest.setdefault(dest, (sha, set()))[1].update(
            str(Path(b[r][k]).parent) for k in ("sch", "pcb", "cpl") if b[r].get(k))
    for dest, (sha, dirs) in by_dest.items():
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
                            b["url"], str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", *sorted(dirs)],
                       check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", sha], check=True)


def matches(violation: dict, catch: dict) -> bool:
    """The finding is the defect: same kind, and the net or one of the refs."""
    if violation.get("kind") != catch["kind"]:
        return False
    if catch.get("net") and violation.get("net") != catch["net"]:
        return False
    refs = set(catch.get("refs") or ())
    return not refs or bool(refs & set(violation.get("refs") or ()))


# KiCad 8's board format, the first with footprint fields as `property`
KICAD8_PCB = 20240108


def _pcb(b: dict, rev: str, repo: Path, empty: Path) -> Path:
    """The board a check reads: the revision's own file, or, for one older
    than KiCad 8 (a `module`-era file, which geom reads no footprints from,
    or a KiCad 6/7 one, whose references are `fp_text` that placelib and
    check_connector_fit skip every footprint over, #1123), a copy `kicad-cli pcb upgrade` brought to the current format, as intake
    does to every board it takes in. The copy sits in the run dir and is
    reused while the source is unchanged."""
    import hashlib
    import re
    src = repo / b[rev]["pcb"]
    head = src.read_text(encoding="utf-8", errors="replace")[:400]
    m = re.search(r"\(version\s+(\d+)\)", head)
    if m and int(m.group(1)) >= KICAD8_PCB:
        return src
    dest = empty.parent / f"{rev}-upgraded.kicad_pcb"
    stamp = dest.with_suffix(".src")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if dest.is_file() and stamp.is_file() and stamp.read_text() == digest:
        return dest
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import env
    cli = env.find_kicad_cli()
    if not cli:
        raise SystemExit(f"{b['id']} {rev}: a pre-KiCad-6 board needs kicad-cli "
                         "to upgrade (bin/hwde env)")
    import shutil
    shutil.copyfile(src, dest)
    r = subprocess.run([str(cli), "pcb", "upgrade", str(dest)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise SystemExit(f"{b['id']} {rev}: kicad-cli pcb upgrade failed: "
                         f"{(r.stderr or r.stdout).strip()[-200:]}")
    stamp.write_text(digest)
    return dest


def _netlist_audit(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["netlist_audit.py", "--sch", str(repo / b[rev]["sch"]),
            "--constraints", str(empty)]


def _check_mate_pins(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_mate_pins.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty)]


def _check_current(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_current.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty)]


def _check_pinouts(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_pinouts.py", "--sch", str(repo / b[rev]["sch"]),
            "--constraints", str(empty)]


def _check_connector_fit(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_connector_fit.py", "--pcb", str(_pcb(b, rev, repo, empty))]


def _check_silk(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_silk.py", "--pcb", str(_pcb(b, rev, repo, empty))]


def _check_wicking(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """Vias in or beside SMD pads, and untented vias under part bodies."""
    return ["check_wicking.py", "--pcb", str(_pcb(b, rev, repo, empty))]


def _check_mounting(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """A board big enough to need screws with no mounting hole (#1088)."""
    return ["check_mounting.py", "--pcb", str(_pcb(b, rev, repo, empty))]


def _check_esd(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board alone: clamps at its external connectors. --parts names
    a folder that does not exist, so the victims' abs max is not read from
    whatever parts/ the foreign repo keeps (#1089)."""
    return ["check_esd.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty), "--parts", str(empty.parent / "no-parts")]


def _check_bom_sync(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_bom_sync.py", "--pcb", str(_pcb(b, rev, repo, empty)), "--live"]


def _check_fp_package(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_fp_package.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--live"]


def _check_thermal(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board alone: with no thermal entry it screens the power ICs
    reference/thermal_parts.yaml knows (#884)."""
    return ["check_thermal.py", "--pcb", str(_pcb(b, rev, repo, empty)),
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
    return ["check_straps.py", "--pcb", str(_pcb(b, rev, repo, empty)),
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
    return ["dfm_check.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--cpl", str(stage / "CPL.csv"), "--no-polarity",
            "--skip", "copper,drill,silk,castellation,zone_fill,netlist"]


# script -> its argv on one revision (empty constraints: what hwde reads
# before anyone has written one)
RUNNERS = {"netlist_audit": _netlist_audit, "check_mate_pins": _check_mate_pins,
           "check_current": _check_current, "check_straps": _check_straps,
           "check_bom_sync": _check_bom_sync,
           "check_fp_package": _check_fp_package,
           "check_connector_fit": _check_connector_fit,
           "check_silk": _check_silk, "check_thermal": _check_thermal,
           "check_pinouts": _check_pinouts, "check_esd": _check_esd,
           "check_wicking": _check_wicking,
           "check_mounting": _check_mounting,
           "dfm": _dfm}


class CheckCrashed(SystemExit):
    """A check that wrote no report, or a report whose status is "error": its
    return code, its error and the tail of its output ride along, so a crash
    is never read as "no finding" (#1189). A check under checklib's contract
    prints its error JSON on stdout with exit 2 and leaves stderr empty."""

    TAIL = 20

    def __init__(self, msg: str, rc: int | None, stderr: str | None,
                 stdout: str | None = None):
        self.rc = rc
        self.check_error = None
        for ln in reversed((stdout or "").splitlines()):
            try:
                self.check_error = json.loads(ln).get("error")
                break
            except (ValueError, AttributeError):
                continue
        self.tail = (stderr or "").rstrip("\n").splitlines()[-self.TAIL:]
        super().__init__(f"{msg} (rc {rc})" + (
            f": {self.check_error}" if self.check_error else "") + "".join(
            f"\n  {ln}" for ln in self.tail))


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
    # a report left by an earlier run must not stand in for this one
    out.unlink(missing_ok=True)
    empty = out.parent / f"constraints{tag}.json"
    empty.write_text(json.dumps(constraints or {}), encoding="utf-8")
    cmd, *args = RUNNERS[script](b, rev, repo_dir(cache, b, rev), empty)
    try:
        p = subprocess.run([sys.executable, str(SCRIPTS / cmd), *args,
                            "--out", str(out)],
                           capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) \
            else e.stderr
        raise CheckCrashed(f"{b['id']} {rev}: {script} timed out after 900 s",
                           None, err)
    if not out.is_file():
        raise CheckCrashed(f"{b['id']} {rev}: {script} wrote no report",
                           p.returncode, p.stderr, p.stdout)
    rep = json.loads(out.read_text(encoding="utf-8"))
    if rep.get("status") == "error":
        raise CheckCrashed(f"{b['id']} {rev}: {script} reported an error",
                           p.returncode, p.stderr,
                           json.dumps({"error": rep.get("error")}))
    return rep.get("violations", [])


def _key(v: dict) -> tuple:
    return (v.get("kind"), v.get("net"), tuple(sorted(v.get("refs") or ())))


def sweep(cache: Path, b: dict) -> dict:
    """Every check the corpus runs, on both revisions, with nothing said about
    the defects: the blind run a holdout board gets before its errata are read
    against the findings. dfm runs only where the revision published a CPL.
    What comes back per check: the rev A findings with no twin on rev B (same
    kind, net and refs), the ones the fix could have removed, and the counts."""
    res: dict = {}
    for script in RUNNERS:
        if script == "dfm" and not (b["rev_a"].get("cpl") and b["rev_b"].get("cpl")):
            continue
        try:
            a, z = (audit(cache, b, rev, script) for rev in ("rev_a", "rev_b"))
        except CheckCrashed as e:
            res[script] = {"error": str(e).splitlines()[0][:300], "rc": e.rc,
                           "check_error": e.check_error,
                           "stderr_tail": e.tail}
            continue
        except (SystemExit, subprocess.SubprocessError, OSError) as e:
            res[script] = {"error": str(e)[:300]}
            continue
        zk = {_key(v) for v in z}
        res[script] = {"rev_a": len(a), "rev_b": len(z),
                       "gone_on_rev_b": [v for v in a if _key(v) not in zk]}
    out = cache / "runs" / b["id"] / "sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    return res


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
                try:
                    found[key] = audit(cache, b, rev, c["script"], cons)
                except CheckCrashed as e:
                    found[key] = e
            if isinstance(found[key], CheckCrashed):
                e = found[key]
                hit = {"crashed": c["script"], "error": str(e).splitlines()[0][:300],
                       "rc": e.rc, "check_error": e.check_error}
                break
            hit[rev] = any(matches(v, c) for v in found[key])
        res[d["id"]] = {"ran": True, **hit}
    (cache / "runs" / b["id"]).mkdir(parents=True, exist_ok=True)
    (cache / "runs" / b["id"] / "result.json").write_text(json.dumps(res, indent=1))
    return res


def outcome(d: dict, r: dict | None) -> str:
    if not d.get("catch"):
        return d["verdict"]
    if d.get("caught_by"):
        how = outcome({k: v for k, v in d.items() if k != "caught_by"}, r)
        return f"{how}, since {d['caught_by']}"
    if not r or not r.get("ran"):
        return f"{d['verdict']} (not run here)"
    if r.get("crashed"):
        return f"crashed: {r['crashed']}"
    if r["rev_a"] and not r["rev_b"]:
        return "caught on rev A, gone on rev B"
    if r["rev_a"]:
        return "REGRESSED: fires on rev B too"
    return "REGRESSED: not found on rev A"


def _board_rows(cache: Path, b: dict, blind: bool) -> list[str]:
    rf = cache / "runs" / b["id"] / "result.json"
    res = json.loads(rf.read_text()) if rf.exists() else {}
    head = "| defect | class | " + ("blind sweep | " if blind else "") + \
        "pcb-flow on rev A | follow-up |"
    lines = [f"### {b['id']}: {b['rev_a']['name']} -> {b['rev_b']['name']}", "",
             f"[{b['url']}]({b['url']}) at `{b['commit'][:12]}`, {b['licence']}; "
             f"errata `{b['source']}`." + (
                 f" Holdout round {b['swept']['round']}, swept blind with "
                 f"pcb-flow `{b['swept']['flow'][:12]}` on "
                 f"{b['swept']['date']}." if blind else ""), "",
             head, "|---|---|" + ("---|" if blind else "") + "---|---|"]
    for d in b["defects"]:
        catch = d.get("catch")
        how = outcome(d, res.get(d["id"]))
        if catch:
            how += f" (`{catch['script']}` {catch['kind']}" + (
                ", with constraints" if catch.get("constraints") else "") + ")"
        lines.append((f"| {d['id']}: {d['summary']} | {d['fault_class']} | "
                      + (f"{d['blind']} | " if blind else "")
                      + f"{how} | {d.get('follow_up') or d.get('note') or ''} |"
                      ).replace("\n", " "))
    return lines + [""]


def _counts(boards: list[dict], field: str) -> tuple[str, int, int]:
    count: dict[str, int] = {}
    for b in boards:
        for d in b["defects"]:
            v = d[field].replace("caught_with_constraints", "caught")
            count[v] = count.get(v, 0) + 1
    total = sum(count.values())
    return (", ".join(f"{v} {k}" for k, v in sorted(count.items())),
            count.get("caught", 0), total)


ROUNDS = """\
## A new holdout round

A holdout board stays blind only until its findings are read against its
errata; after that its defects are known, and a rule written later is no
longer tested by it. So each round of rules is scored on a fresh holdout
round, pinned and swept before those rules are:

1. Before the round's rules are scored, an agent who has not read the
   boards picks about 20 errata from boards nobody here has read, chosen as
   the earlier rounds were: a public KiCad repository with a licence
   (`fetch` upgrades a pre-KiCad-6 board with kicad-cli), where a later
   revision fixes a defect the designer's own errata quote, both revisions
   pinned to a commit, and no repository an earlier round or the training
   set holds.
2. Each goes in the round's own sealed file, `holdout/round-N.yaml`
   (a `holdout:` list), with `swept: {flow: SHA, date, round: N}`, SHA
   being main as it stands, and its defects with the designer's quote,
   before anyone reads the findings. `errata_corpus.py sweep ID...` runs
   every check on both revisions; `load` merges the sealed files, so
   `run` and `table` see them too.
3. Each defect's `blind` is then judged from that sweep alone and never
   changes. Rules written after it may still catch it (`verdict`, with the
   rule in `caught_by`); a rule seeded from a holdout defect moves its board
   to `boards:`.
4. Report three numbers: training, the earlier rounds with the rules
   written since (known boards, not an independent test), and round N
   blind. Round N is known once read: the next round of rules gets round
   N+1.
5. Add the round's misses to `docs/miss-taxonomy.md`, one class per check,
   and re-rank it: the next rules go to the top classes. While the round is
   sealed, its member lines go in `holdout/round-N-members.md`, and the
   taxonomy says only how many members each class has from it.

**Keeping a sealed round blind.** Until a round is unsealed, its defects
live only in the repo's top `holdout/` folder: `holdout/round-N.yaml`,
`holdout/round-N-members.md` and `holdout/round-N-errata.md` (written by
`table`). It is outside `reference/` and `docs/`, so a grep there for a part
or a check doesn't reach them. Rule builders open none of these, nor the round's
boards or errata upstream, and their briefs quote only the taxonomy's
Ranked table. Only the round's scorers read them. `tests/test_holdout_sealed.py`
fails if a sealed defect id appears in any other tracked file. Unsealing a
round (when a newer round replaces it as the blind test) moves its boards
into `reference/errata_corpus.yaml` and its members into the taxonomy.
"""


SEALED_NOTE = (
    "Sealed: its boards and defects are in `holdout/round-{r}-errata.md`,"
    " for the round's scorers only. Rule builders don't open it, or the"
    " round's boards and errata; see \"A new holdout round\".")


def sealed_table(cache: Path, holdout: list[dict], r: int) -> str:
    """The per-board section of a sealed round, kept out of the main doc."""
    lines = [f"# Holdout round {r} (sealed, scorers only)", "",
             "Rule builders: stop here. This file names the round's defects, and",
             "a rule written after reading it is no longer tested by the round.",
             "Written by `bin/pcb-flow errata_corpus table` from",
             f"`holdout/round-{r}.yaml`.", ""]
    for b in holdout:
        if b["swept"]["round"] == r:
            lines += _board_rows(cache, b, True)
    return "\n".join(lines)


def table(cache: Path, boards: list[dict], holdout: list[dict] | None = None,
          sealed: set[int] | frozenset[int] = frozenset()) -> str:
    holdout = holdout or []
    lines = ["# The real-errata corpus", "",
             "Public boards whose next revision fixed a defect the first one shipped with,",
             "quoted from the designer's own errata (`reference/errata_corpus.yaml`), and",
             "what pcb-flow's checks do with the first revision. Written by",
             "`bin/pcb-flow errata_corpus table` after `fetch`, `run` and `sweep`.", "",
             "Two sets. **Training**: most of its catches were written from the defect",
             "they catch, so its count says how well the checks fit what they were built",
             "from, not how they do on a new board. **Holdout**: boards no rule was written",
             "from, swept with every check before their findings were read against the",
             "errata; the *blind* column is that first judgement and never changes. It is",
             "the number that says how the flow does on a board it has never seen, and",
             "only for the round's first reading: each round is swept once.", ""]
    tr, tc, tn = _counts(boards, "verdict")
    lines += ["## Count", "", f"- Training: {tc} of {tn} caught ({tr})."]
    rounds = sorted({b["swept"]["round"] for b in holdout})
    for r in rounds:
        hr = [b for b in holdout if b["swept"]["round"] == r]
        hb, bc, bn = _counts(hr, "blind")
        hv, vc, _ = _counts(hr, "verdict")
        lines += [f"- Holdout round {r}, blind: {bc} of {bn} caught ({hb}).",
                  f"- Holdout round {r}, with the rules written since: "
                  f"{vc} of {bn} caught ({hv})."]
    lines += ["", ROUNDS, "## Training set", ""]
    for b in boards:
        lines += _board_rows(cache, b, False)
    for r in rounds:
        lines += [f"## Holdout round {r}", ""]
        if r in sealed:
            lines += [SEALED_NOTE.format(r=r), ""]
            continue
        for b in holdout:
            if b["swept"]["round"] == r:
                lines += _board_rows(cache, b, True)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["fetch", "run", "sweep", "table"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--cache")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(DOC))
    a = ap.parse_args(argv)
    cache = cache_root(a.cache)
    man = Path(a.manifest)
    if a.cmd == "table":
        holdout = load(man, "holdout")
        sealed = set(sealed_files(man))
        out = Path(a.out)
        out.write_text(table(cache, load(man), holdout, sealed), encoding="utf-8")
        for r in sorted(sealed):
            f = out.parent.parent / "holdout" / f"round-{r}-errata.md"
            f.parent.mkdir(exist_ok=True)
            f.write_text(sealed_table(cache, holdout, r), encoding="utf-8")
        return 0
    if a.cmd == "sweep":
        # unvalidated: a holdout board is fetched and swept before its
        # defects are judged, so before it has its blind verdicts
        raw = _raw(man, "holdout")
        pairs, errored = 0, []
        for b in raw:
            if a.ids and b["id"] not in a.ids:
                continue
            fetch(cache, b)
            for script, r in sweep(cache, b).items():
                print(b["id"], script, *([f"crashed: {script}"] if "error" in r
                                         else []), json.dumps(r))
                pairs += 1
                if "error" in r:
                    errored.append(f"{b['id']} {script}")
        # a crashed check is a hole in the sweep, not a clean pass (#1189)
        print(f"sweep: {pairs} (board, check) pairs, {len(errored)} errored"
              + (": " + ", ".join(errored) if errored else ""))
        return 0
    boards = [b for b in load_all(man) if not a.ids or b["id"] in a.ids]
    bad = 0
    for b in boards:
        if a.cmd == "fetch":
            fetch(cache, b)
        else:
            for did, r in run(cache, b).items():
                print(did, (f"crashed: {r['crashed']} " if r.get("crashed")
                            else "") + json.dumps(r))
                bad += r.get("ran", False) and (
                    bool(r.get("crashed")) or not (r["rev_a"] and not r["rev_b"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
