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
    (a sealed round's boards only with --sealed N, from --sealed-cache)
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
parts/, so check_esd reads no victim's abs max), check_ratings on the board
(each pin rated from hwde's library extractions by its LCSC number, then
from the set's batch readings when abs_max_batch.py has made them, rails
from net names: no parts/ and no power tree), check_pinouts on the
schematic, check_bom_sync on the board with --live (the package of each LCSC part
comes from JLC's record, as nothing in the foreign tree holds it),
check_fp_package on the board with --live (each part's package from JLC's
record of its LCSC/JLC number or of the exact part its MPN names), fp_verify's
board mode (pad numbering of each placed footprint against the record its LCSC
number or MPN finds in hwde's extractions), check_thermal on the board
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
from collections import Counter
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


def sealed_cache_root(arg: str | None, cache: Path) -> Path:
    """Where sealed-round checkouts and their sweep reports live: apart
    from the training cache, so a builder who globs that cache's `repos/`
    never reaches a sealed board (default ~/.cache/hwde-holdout, or
    HWDE_HOLDOUT_CACHE)."""
    c = Path(arg or os.environ.get("HWDE_HOLDOUT_CACHE")
             or Path.home() / ".cache" / "hwde-holdout").resolve()
    if c == ROOT or ROOT in c.parents:
        raise SystemExit(f"sealed cache {c} is inside the repo")
    if c == cache or cache in c.parents or c in cache.parents:
        raise SystemExit(f"sealed cache {c} overlaps the training cache {cache}")
    return c


def sealed_in(cache: Path, ids) -> int:
    """How many sealed-round boards have a checkout or a report in `cache`
    (a count, never the ids)."""
    names = {p.name.split("@")[0] for d in ("repos", "runs")
             if (cache / d).is_dir() for p in (cache / d).iterdir()}
    return len(names & set(ids))


def guard_training_cache(cache: Path, ids) -> None:
    """Refuse to work from a training cache that holds a sealed board.

    A checkout made before a round was sealed (or from a branch older than
    the seal) still sits where a builder's glob reaches it; every command
    stops on it until it is moved to the sealed cache."""
    n = sealed_in(cache, ids)
    if n:
        raise SystemExit(f"{n} sealed-round board(s) in the training cache {cache}; "
                         "a scorer moves them to the sealed cache (see \"A new "
                         "holdout round\"); builders don't open them")


def sealed_rounds(path: Path = MANIFEST) -> dict[str, int]:
    """Board id -> round, for every board of a sealed round."""
    out = {}
    for n, f in sealed_files(path).items():
        for b in yaml.safe_load(f.read_text(encoding="utf-8")).get("holdout") or []:
            out[b["id"]] = n
    return out


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


def _upgraded(b: dict, rev: str, repo: Path, empty: Path) -> Path:
    """The revision's own file, or, for one older
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


def _netlist(b: dict, rev: str, repo: Path, empty: Path) -> Path:
    """The netlist of the revision's schematic, in the run dir: kicad-cli's
    export, or for a KiCad 4/5 schematic (which kicad-cli exports empty)
    legacy_net's. Kept while the schematic is unchanged."""
    import hashlib
    sys.path.insert(0, str(SCRIPTS / "lib"))
    import env
    import legacy_net
    sch = repo / b[rev]["sch"]
    net = empty.parent / f"{rev}.net"
    stamp = net.with_suffix(".net.src")
    digest = hashlib.sha256(str(sch).encode() + sch.read_bytes()).hexdigest()
    if net.is_file() and stamp.is_file() and stamp.read_text() == digest:
        return net
    stamp.unlink(missing_ok=True)
    if legacy_net.is_legacy(sch):
        r = legacy_net.netlist(sch, net, pcb=repo / b[rev]["pcb"])
        if r["status"] != "pass":
            raise SystemExit(f"{b['id']} {rev}: {r['error']}")
    else:
        cli = env.find_kicad_cli()
        if not cli:
            raise SystemExit(f"{b['id']} {rev}: a netlist needs kicad-cli "
                             "(bin/hwde env)")
        subprocess.run([str(cli), "sch", "export", "netlist", "-o", str(net),
                        str(sch)], capture_output=True, text=True,
                       timeout=900, check=True)
    stamp.write_text(digest)
    return net


def _pcb(b: dict, rev: str, repo: Path, empty: Path) -> Path:
    """The board a check reads: _pins_pcb's (the revision's file brought to
    KiCad 8, with its schematic's pin names), then with the part numbers the
    board's own sources name for the footprints that carry none, as
    partnum_backfill gives them: the schematic's symbol fields, the BOM files
    in the board's folder, parts.json. Every board gets the same step; it
    only adds an LCSC or MPN field where the footprint has no such field.
    The other revision's own sources come last (partnum_backfill's
    other_revision: the same ref, where its footprint and Value match), so
    a board whose BOM ships with one revision only gets its part numbers
    from the same sources on both, and a finding does not look gone or new
    only because of the BOM.
    The copy sits in the run dir (`{rev}-partnum.kicad_pcb`, with what
    partnum_backfill said in `{rev}-partnum.json`), nothing is written into
    the checkout, and it is reused while its inputs are unchanged."""
    import hashlib
    base = _pins_pcb(b, rev, repo, empty)
    dest = empty.parent / f"{rev}-partnum.kicad_pcb"
    report = empty.parent / f"{rev}-partnum.json"
    stamp = dest.with_suffix(".src")
    net = None
    sch = repo / b[rev]["sch"] if b[rev].get("sch") else None
    if sch is not None and sch.is_file():
        try:
            net = _netlist(b, rev, repo, empty)
        except (SystemExit, subprocess.SubprocessError, OSError):
            net = None
    else:
        sch = None
    other = _other_rev(b, rev, empty)
    digest = hashlib.sha256(
        base.read_bytes() + b"\0" + str(repo / b[rev]["pcb"]).encode()
        + b"\0" + (net.read_bytes() if net else b"")
        + b"\0" + json.dumps(other.get("key")).encode()
        + b"\0" + (SCRIPTS / "partnum_backfill.py").read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text().split("\n")[0] == digest:
        return dest if stamp.read_text().split("\n")[1:2] == ["written"] \
            else base
    stamp.unlink(missing_ok=True)
    dest.unlink(missing_ok=True)
    cmd = [sys.executable, str(SCRIPTS / "partnum_backfill.py"),
           "--pcb", str(base), "--src-pcb", str(repo / b[rev]["pcb"]),
           "--repo", str(repo), "--out", str(dest),
           "--out-report", str(report)]
    if net:
        cmd += ["--netlist", str(net)]
    if sch:
        cmd += ["--sch", str(sch)]
    cmd += other.get("args", [])
    subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    try:
        status = json.loads(report.read_text(encoding="utf-8"))["status"]
    except (OSError, ValueError, KeyError):
        status = "error"
    if status != "written" or not dest.is_file():
        dest.unlink(missing_ok=True)
        status = status if status != "written" else "error"
    stamp.write_text(f"{digest}\n{status}")
    return dest if status == "written" else base


def _other_rev(b: dict, rev: str, empty: Path) -> dict:
    """partnum_backfill's --other-* arguments for the board's other
    revision (its pin-named board, checkout, netlist and schematic), and a
    key of their contents for _pcb's reuse stamp; {} when there is none or
    it cannot be read."""
    import hashlib
    orev = "rev_b" if rev == "rev_a" else "rev_a"
    if not (b.get(orev) or {}).get("pcb"):
        return {}
    cache = empty.parent.parent.parent
    orepo = repo_dir(cache, b, orev)
    if not (orepo / b[orev]["pcb"]).is_file():
        return {}
    try:
        opcb = _pins_pcb(b, orev, orepo, empty)
    except (SystemExit, subprocess.SubprocessError, OSError):
        return {}
    args = ["--other-pcb", str(opcb),
            "--other-src-pcb", str(orepo / b[orev]["pcb"]),
            "--other-repo", str(orepo)]
    key = [hashlib.sha256(opcb.read_bytes()).hexdigest(),
           str(orepo / b[orev]["pcb"])]
    osch = orepo / b[orev]["sch"] if b[orev].get("sch") else None
    if osch is not None and osch.is_file():
        try:
            onet = _netlist(b, orev, orepo, empty)
            args += ["--other-netlist", str(onet)]
            key.append(hashlib.sha256(onet.read_bytes()).hexdigest())
        except (SystemExit, subprocess.SubprocessError, OSError):
            pass
        args += ["--other-sch", str(osch)]
    return {"args": args, "key": key}


def _pins_pcb(b: dict, rev: str, repo: Path, empty: Path) -> Path:
    """The revision's file (brought to KiCad 8 by
    _upgraded where older), with the pin names its own schematic gives the
    pads that carry none, as pin_backfill gives a board board_init made
    before #1220. Every board gets the same step, from its own schematic
    only: pin_backfill names a pad only where the board's net agrees with
    the schematic's, and writes nothing when under half agree. The named
    copy sits in the run dir (`{rev}-pins.kicad_pcb`, with what pin_backfill
    said in `{rev}-pins.json`), nothing is written into the checkout, and it
    is reused while the board, the schematic and pin_backfill are unchanged.
    A revision with no schematic, or one whose netlist will not export, is
    read without the names."""
    import hashlib
    base = _upgraded(b, rev, repo, empty)
    if not b[rev].get("sch") or not (repo / b[rev]["sch"]).is_file():
        return base
    dest = empty.parent / f"{rev}-pins.kicad_pcb"
    report = empty.parent / f"{rev}-pins.json"
    stamp = dest.with_suffix(".src")
    sch = repo / b[rev]["sch"]
    digest = hashlib.sha256(
        base.read_bytes() + b"\0" + str(sch).encode() + sch.read_bytes()
        + b"\0" + (SCRIPTS / "pin_backfill.py").read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text().split("\n")[0] == digest:
        return dest if stamp.read_text().split("\n")[1:2] == ["written"] \
            else base
    stamp.unlink(missing_ok=True)
    dest.unlink(missing_ok=True)
    try:
        net = _netlist(b, rev, repo, empty)
    except (SystemExit, subprocess.SubprocessError, OSError) as e:
        report.write_text(json.dumps({"status": "no_netlist",
                                      "error": str(e)[:300]}))
        stamp.write_text(f"{digest}\nno_netlist")
        return base
    subprocess.run([sys.executable, str(SCRIPTS / "pin_backfill.py"),
                    "--pcb", str(base), "--netlist", str(net),
                    "--out", str(dest), "--out-report", str(report)],
                   capture_output=True, text=True, timeout=900)
    try:
        status = json.loads(report.read_text(encoding="utf-8"))["status"]
    except (OSError, ValueError, KeyError):
        status = "error"
    if status != "written" or not dest.is_file():
        dest.unlink(missing_ok=True)
        status = status if status != "written" else "error"
    stamp.write_text(f"{digest}\n{status}")
    return dest if status == "written" else base


def _netlist_audit(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["netlist_audit.py", "--sch", str(repo / b[rev]["sch"]),
            "--constraints", str(empty)]


def _check_mate_pins(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_mate_pins.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty)]


def board_constraints(b: dict, rev: str, repo: Path) -> Path | None:
    """The revision's own constraints.json, if its project ships one: next to
    the board file, else at the checkout's root. The same lookup on every
    board, with nothing written per board; none found, none used."""
    pcb = repo / b[rev]["pcb"]
    for d in (pcb.parent, repo):
        f = d / "constraints.json"
        if f.is_file():
            return f
    return None


def _check_current(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    # a catch's own constraints (a non-empty file) win; otherwise the
    # board's shipped constraints.json, where one exists
    cons = empty
    try:
        stated = json.loads(empty.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        stated = None
    if not stated:
        cons = board_constraints(b, rev, repo) or empty
    return ["check_current.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(cons)]


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


def _check_ratings(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """Each pin's rating against its net, rated from hwde's library
    extractions only: --parts is an empty folder in the run dir (the foreign
    repo's parts/ is not read, and check_ratings needs the folder to exist)
    and --power-tree names no file, so a rail's volts come from its net name.
    audit() runs it with HWDE_LEDGER_WRITE=0, so no ledger.json lands in the
    cached repo. It runs through errata_ratings.py, the same check with each
    rated part's source added (rated_from), for the coverage count.
    When abs_max_batch.py has read the board's set (#1339), its folder
    (abs_max_dir) is passed as --extractions: hwde's library still wins."""
    parts = empty.parent / "ratings-no-parts"
    parts.mkdir(exist_ok=True)
    ext = abs_max_dir(repo.parent.parent, b)
    return ["errata_ratings.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty), "--parts", str(parts),
            "--power-tree", str(empty.parent / "no-power-tree.md"),
            *(["--extractions", str(ext)] if ext.is_dir() else [])]


def set_label(b: dict) -> str:
    """The board's set: `training`, or `round-N` for a holdout board swept
    in round N (sealed or not)."""
    r = (b.get("swept") or {}).get("round")
    return f"round-{r}" if r else "training"


def abs_max_dir(cache: Path, b: dict) -> Path:
    """Where abs_max_batch.py writes the extractions read for the board's
    set, in the cache that holds the board's checkout."""
    return cache / "abs-max" / set_label(b) / "ext"


def _check_bom_sync(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_bom_sync.py", "--pcb", str(_pcb(b, rev, repo, empty)), "--live"]


def _check_fp_package(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_fp_package.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--live"]


def _check_fp_geometry(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    return ["check_fp_geometry.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--live"]


def _fp_verify(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """fp_verify's board mode (#1285): the pad-numbering checks on each
    footprint as placed, against the record its LCSC number or MPN finds in
    hwde's extractions (no parts/ in a foreign tree)."""
    return ["fp_verify.py", "--pcb", str(_pcb(b, rev, repo, empty))]


def _check_thermal(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board alone: with no thermal entry it screens the power ICs
    reference/thermal_parts.yaml knows (#884)."""
    return ["check_thermal.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--constraints", str(empty)]


def _check_straps(b: dict, rev: str, repo: Path, empty: Path) -> list[str]:
    """On the board, with the netlist kicad-cli exports from its schematic
    (what the pipeline puts beside an hwde board)."""
    return ["check_straps.py", "--pcb", str(_pcb(b, rev, repo, empty)),
            "--netlist", str(_netlist(b, rev, repo, empty))]


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
           "check_fp_geometry": _check_fp_geometry,
           "check_connector_fit": _check_connector_fit,
           "check_silk": _check_silk, "check_thermal": _check_thermal,
           "check_pinouts": _check_pinouts, "check_esd": _check_esd,
           "check_ratings": _check_ratings,
           "check_wicking": _check_wicking,
           "check_mounting": _check_mounting,
           "fp_verify": _fp_verify,
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
        # nothing is written into the foreign tree (check_ratings' ledger)
        p = subprocess.run([sys.executable, str(SCRIPTS / cmd), *args,
                            "--out", str(out)],
                           capture_output=True, text=True, timeout=900,
                           env={**os.environ, "HWDE_LEDGER_WRITE": "0"})
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


# Findings that say what a check could not judge, not that something is wrong.
# The sweep lists them under `coverage_gone_on_rev_b`, apart from the triage
# rows in `gone_on_rev_b`; a part list that changed between revisions is
# enough to make one disappear on rev B.
# Kinds that only say what a check couldn't judge, never that the board is
# wrong: rating_no_attributes is an error because it blocks verify until the
# parts row is filled, but it judges nothing about the part.
COVERAGE_KINDS = {"check_ratings": {
    "rating_unrated", "rating_no_datasheet_read", "rating_datasheet_unassessed",
    "rating_regulator_output_unjudged", "rating_no_attributes"}}


def _n(v) -> int:
    """A report field as a count: a list or dict by its length, a number as is."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, (list, tuple, dict)):
        return len(v)
    return 0


def _rows(rep: dict) -> list[dict]:
    return [c for c in rep.get("checked") or () if isinstance(c, dict)]


def _sum(rep: dict, *keys: str) -> int:
    return sum(_n(c.get(k)) for c in _rows(rep) for k in keys)


# What each check's report shows it actually judged on a board (items it
# checked, parts it compared): a check that says nothing_to_judge only when it
# finds no part of its kind otherwise counts as judging a board it looked at
# and judged nothing on. Read only from the report; the checks are untouched.
EVIDENCE = {
    "check_current": lambda r: _n(r.get("checked")),
    "check_thermal": lambda r: _n(r.get("checked")),
    "check_esd": lambda r: _sum(r, "connectors"),
    "check_silk": lambda r: _sum(r, "silk_items", "refdes_checked"),
    "check_mounting": lambda r: _n(r.get("checked")),
    "check_wicking": lambda r: _sum(r, "vias", "smd_pads"),
    "check_straps": lambda r: _sum(r, "mcus"),
    "check_fp_geometry": lambda r: sum(_n(v) for v in (r.get("judged") or {}).values())
    if isinstance(r.get("judged"), dict) else _n(r.get("judged")),
    "check_fp_package": lambda r: _n(r.get("judged")),
    "fp_verify": lambda r: sum(_n(v) for v in (r.get("judged") or {}).values()),
    "check_ratings": lambda r: _n(r.get("pins_checked")) + _n(
        (r.get("operating_range") or {}).get("parts_checked"))
    + _n(r.get("listing_comparison")) + _n(r.get("parts_rated_unextracted")),
    "check_pinouts": lambda r: _n(r.get("connectors")),
    "check_mate_pins": lambda r: sum(_n(r.get(k)) for k in
                                     ("pairs", "stated_mates", "standard_headers",
                                      "unkeyed_groups")),
    "check_connector_fit": lambda r: _n(r.get("edges")) + sum(
        1 for p in r.get("parts") or () if isinstance(p, dict) and p.get("pins")),
    "check_bom_sync": lambda r: sum(1 for v in (r.get("legs") or {}).values() if v),
    "netlist_audit": lambda r: _n(r.get("pins_connected")) + sum(
        _n(v) for k, v in r.items()
        if k.endswith(("_checked", "_judged")) and isinstance(v, int)),
    "dfm": lambda r: _n((r.get("coverage") or {}).get("ran")),
}

# Findings that say a check couldn't judge something, for the coverage count
# only (the triage rows keep them): they don't show the check judged a board.
NOT_A_JUDGEMENT = {**COVERAGE_KINDS, "check_straps": {"mcu_unknown"}}


def _not_judged_count(nj) -> int:
    if isinstance(nj, dict):
        return sum(len(v) if isinstance(v, (list, tuple)) else
                   int(v) if isinstance(v, int) else 1 for v in nj.values())
    if isinstance(nj, (list, tuple)):
        return len(nj)
    return 1 if nj else 0


def coverage(rep: dict, script: str | None = None) -> dict:
    """What a check's report says it could judge on a board. `judged` only when
    it made at least one finding (not a can't-judge note) or its report shows
    something it judged (EVIDENCE); `nothing_to_judge` (its reason) when it
    says so; `judged_nothing` when it judged nothing and didn't say why.
    `not_judged` counts the items it named but couldn't judge (a list, or a
    dict of reason -> items or a count). For the footprint checks,
    `parts_judged` / `parts_not_judged` count parts; for check_ratings,
    `parts_judged` and `parts_by_source` count the parts it rated."""
    n = _not_judged_count(rep.get("not_judged"))
    out: dict = {"not_judged": n}
    if script in ("check_fp_geometry", "check_fp_package", "fp_verify"):
        out["parts_judged"] = EVIDENCE[script](rep)
        out["parts_not_judged"] = n
    if script == "check_ratings" and isinstance(rep.get("rated_from"), dict):
        # every part it compared with a known voltage, by source
        # (errata_ratings.py): pins_checked counts datasheet pins only
        out["parts_judged"] = len(rep["rated_from"])
        out["parts_by_source"] = dict(sorted(Counter(
            rep["rated_from"].values()).items()))
    if rep.get("nothing_to_judge"):
        return {"status": "nothing_to_judge",
                "reason": str(rep["nothing_to_judge"])[:200], **out}
    if script is None or script not in EVIDENCE:
        return {"status": "judged", **out}
    skip = NOT_A_JUDGEMENT.get(script, set())
    found = any(v.get("kind") not in skip for v in rep.get("violations") or ()
                if isinstance(v, dict))
    judged = found or EVIDENCE[script](rep) > 0
    return {"status": "judged" if judged else "judged_nothing", **out}


def coverage_line(rows: list[tuple[str, str, dict]]) -> list[str]:
    """Per check, over the swept boards (rev A): how many it judged, how many
    gave it nothing to judge, how many had items it couldn't judge, and how
    many it crashed on. Shows where a score can't move: a check that judges
    nothing on a set can't catch anything on it."""
    per: dict[str, dict[str, int]] = {}
    for _bid, script, r in rows:
        c = per.setdefault(script, {"judged": 0, "nothing_to_judge": 0,
                                    "judged_nothing": 0, "with_not_judged": 0,
                                    "crashed": 0})
        if "error" in r:
            c["crashed"] += 1
            continue
        cov = r.get("coverage")
        if not cov:
            continue
        c[cov["status"]] += 1
        c["with_not_judged"] += bool(cov.get("not_judged"))
        if "parts_judged" in cov:
            c["parts_judged"] = c.get("parts_judged", 0) + cov["parts_judged"]
        if "parts_not_judged" in cov:
            c["parts_not_judged"] = (c.get("parts_not_judged", 0)
                                     + cov["parts_not_judged"])
        for src, n in (cov.get("parts_by_source") or {}).items():
            by = c.setdefault("by_source", {})
            by[src] = by.get(src, 0) + n

    def parts(c: dict) -> str:
        if "parts_judged" not in c:
            return ""
        out = f"; parts judged {c['parts_judged']}"
        if "by_source" in c:
            out += " (" + ", ".join(f"{k} {n}" for k, n in sorted(
                c["by_source"].items(), key=lambda kv: (-kv[1], kv[0]))) + ")"
        if "parts_not_judged" in c:
            out += f", parts not_judged {c['parts_not_judged']}"
        return out
    return [f"coverage {s}: judged {c['judged']}, nothing_to_judge "
            f"{c['nothing_to_judge']}, judged nothing (unsaid) "
            f"{c['judged_nothing']}, with not_judged items "
            f"{c['with_not_judged']}, crashed {c['crashed']}" + parts(c)
            for s, c in sorted(per.items())]


# A word kept in a blocker label: a lowercase word, a lowercase kind
# (snake_case), a lowercase path to a reference table, or a short all-caps
# word with no digit (MCU, JLC). Part numbers, refs, nets, footprint names,
# quoted and parenthesised text are dropped, so a label carries no board's
# names and can go to builders as a count.
_WORD = re.compile(r"^(?:[a-z][a-z'-]*[a-z]|[a-z]|[a-z]+(?:_[a-z]+)+"
                   r"|[a-z_/]+\.(?:yaml|json|csv|md)|[A-Z]{2,5}(?:s|'s)?|[A-Z][a-z'-]+)"
                   r"[,.;:]?$")


def blocker_label(text) -> str:
    """A reason a check gave, as a label that names nothing on the board."""
    t = str(text or "")
    t = re.sub(r"\([^()]*\)|\[[^\[\]]*\]|(?<![A-Za-z])'[^']*'(?![A-Za-z])"
               r"|\"[^\"]*\"", " ", t)
    words = [w for w in t.split() if _WORD.match(w)]
    out = " ".join(words).strip(" ,.;:")
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", out)
    # a name scrubbed off the end leaves its preposition dangling
    out = re.sub(r"(?:\s+(?:on|for|in|of|with|to|at|by|from|a|an|the|and|or))+$",
                 "", out).strip(" ,.;:")
    return out[:120].rsplit(" ", 1)[0] if len(out) > 120 else (out or "unlabelled")


def _item_label(item) -> str:
    """One not_judged item's reason: a dict's reason/why/kind; a string
    `kind: ...` by its kind, `REFS (part): why` by its why."""
    if isinstance(item, dict):
        return blocker_label(item.get("reason") or item.get("why")
                             or item.get("kind") or "")
    head, sep, tail = str(item).partition(": ")
    kind = head.split(" ", 1)[0]
    if sep and re.match(r"^[a-z]+(?:_[a-z]+)+$", kind):
        return kind
    if sep and re.match(r"^[a-z][a-z_ ]*$", head):
        return blocker_label(head)
    return blocker_label(tail if sep else head)


def blockers(rep: dict | None, cov: dict | None, crash: dict | None = None,
             pins: str | None = None) -> dict:
    """Why a check judged less than all of one board (rev A), from its own
    report: `board` is why it judged nothing there (its nothing_to_judge
    reason, "no reason given" when it gave none, "timeout" or "crashed");
    `items` counts the items it named but couldn't judge, by reason;
    `inputs` notes what the harness couldn't give it (pin names the
    schematic couldn't supply), on a board it didn't judge."""
    out: dict = {"board": None, "items": {}}
    if crash is not None:
        out["board"] = "timeout" if "timed out" in str(crash.get("error")) \
            else "crashed"
    elif cov and cov.get("status") == "nothing_to_judge":
        out["board"] = blocker_label((rep or {}).get("nothing_to_judge")
                                     or cov.get("reason"))
    elif cov and cov.get("status") == "judged_nothing":
        out["board"] = "no reason given"
    nj = (rep or {}).get("not_judged")
    items: dict[str, int] = {}
    if isinstance(nj, dict):
        for k, v in nj.items():
            n = len(v) if isinstance(v, (list, tuple, dict)) else \
                int(v) if isinstance(v, int) and not isinstance(v, bool) else 1
            items[blocker_label(k)] = items.get(blocker_label(k), 0) + n
    elif isinstance(nj, (list, tuple)):
        for it in nj:
            k = _item_label(it)
            items[k] = items.get(k, 0) + 1
    elif nj:
        items["unlabelled"] = 1
    out["items"] = items
    if out["board"] and pins in ("no_netlist", "error", "mismatch", "no_source"):
        out["inputs"] = [f"pin names: {pins}"]
    return out


def _pin_status(cache: Path, b: dict, rev: str = "rev_a") -> str | None:
    """What the pin-name step said for a revision (its stamp's second line)."""
    stamp = cache / "runs" / b["id"] / f"{rev}-pins.src"
    try:
        lines = stamp.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    return lines[1] if len(lines) > 1 else None


def blocker_lines(rows: list[tuple[str, str, dict]]) -> list[str]:
    """Per check, over the swept boards: how many boards each reason kept
    it from judging (a check that gave none counts under "no reason given"),
    and how many items it named but couldn't judge, by reason. Counts and
    labels only, no board names: this reaches builders' logs."""
    per: dict[str, tuple[dict, dict, dict, dict]] = {}
    for _bid, script, r in rows:
        bl = r.get("blockers")
        if not bl:
            continue
        brd, its, on, inp = per.setdefault(script, ({}, {}, {}, {}))
        if bl.get("board"):
            brd[bl["board"]] = brd.get(bl["board"], 0) + 1
        for k, n in (bl.get("items") or {}).items():
            its[k] = its.get(k, 0) + n
            on[k] = on.get(k, 0) + 1
        for k in bl.get("inputs") or ():
            inp[k] = inp.get(k, 0) + 1

    def order(d: dict) -> list:
        return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    out = []
    for s, (brd, its, on, inp) in sorted(per.items()):
        out.append(f"blockers {s}: boards not judged: "
                   + ("; ".join(f"{k} {n}" for k, n in order(brd)) or "none")
                   + (" | inputs missing: " + "; ".join(
                       f"{k} {n}" for k, n in order(inp)) if inp else "")
                   + " | items not judged: "
                   + ("; ".join(f"{k} {n} on {on[k]} board(s)"
                                for k, n in order(its)) or "none"))
    return out


def sweep(cache: Path, b: dict) -> dict:
    """Every check the corpus runs, on both revisions, with nothing said about
    the defects: the blind run a holdout board gets before its errata are read
    against the findings. dfm runs only where the revision published a CPL.
    What comes back per check: the rev A findings with no twin on rev B (same
    kind, net and refs), the ones the fix could have removed, and the counts.
    A check's coverage notes (COVERAGE_KINDS) go under coverage_gone_on_rev_b,
    not among those rows."""
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
            res[script]["blockers"] = blockers(None, None, res[script],
                                               _pin_status(cache, b))
            continue
        except (SystemExit, subprocess.SubprocessError, OSError) as e:
            res[script] = {"error": str(e)[:300]}
            res[script]["blockers"] = blockers(None, None, res[script],
                                               _pin_status(cache, b))
            continue
        zk = {_key(v) for v in z}
        gone = [v for v in a if _key(v) not in zk]
        cov = COVERAGE_KINDS.get(script, set())
        res[script] = {"rev_a": len(a), "rev_b": len(z),
                       "gone_on_rev_b": [v for v in gone if v.get("kind") not in cov]}
        if cov:
            res[script]["coverage_gone_on_rev_b"] = [
                v for v in gone if v.get("kind") in cov]
        rep = cache / "runs" / b["id"] / f"rev_a-{script}.json"
        try:
            report = json.loads(rep.read_text(encoding="utf-8"))
            res[script]["coverage"] = coverage(report, script)
            res[script]["blockers"] = blockers(
                report, res[script]["coverage"], pins=_pin_status(cache, b))
        except (OSError, ValueError):
            pass
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
   before anyone reads the findings. `errata_corpus.py sweep --sealed N
   ID...` runs every check on both revisions; `load` merges the sealed
   files, so `run` and `table` see them too. A sealed round's boards are
   left out of `fetch`, `run` and `sweep` unless `--sealed N` asks for them,
   and they live in their own cache (`--sealed-cache`, default
   ~/.cache/hwde-holdout), apart from the training cache. To have
   check_ratings read the round's ICs' absolute-maximum ratings, run
   `abs_max_batch.py --round N` (`--sealed N` for a sealed round) before
   the sweep: it prints counts only, and the sweep reads its folder.
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
Ranked table. Only the round's scorers read them. A builder's own corpus
sweep globs the training cache's `repos/` for training boards only, and
never passes `--sealed` or opens the sealed cache. `tests/test_holdout_sealed.py`
fails if a sealed defect id appears in any other tracked file, or if a
sealed board is reachable from the default sweep or sits in the training
cache. Every command also stops, with a count and no ids, when its
training cache (`--cache`, HWDE_ERRATA_CACHE or the default) holds a sealed
board's checkout or report: a copy of the cache taken before a round was
sealed still carries the round's boards. Unsealing a
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
    ap.add_argument("--sealed", type=int, action="append", default=[], metavar="N",
                    help="also take sealed round N's boards (scorers only), "
                         "from the sealed cache")
    ap.add_argument("--sealed-cache")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--out", default=str(DOC))
    a = ap.parse_args(argv)
    cache = cache_root(a.cache)
    scache = sealed_cache_root(a.sealed_cache, cache)
    man = Path(a.manifest)
    rounds = sealed_rounds(man)
    guard_training_cache(cache, rounds)

    def where(b: dict) -> Path:
        return scache if b["id"] in rounds else cache

    left_out = []

    def wanted(b: dict) -> bool:
        if a.ids and b["id"] not in a.ids:
            return False
        if b["id"] in rounds and rounds[b["id"]] not in a.sealed:
            left_out.append(b["id"])
            return False
        return True

    def note_left_out() -> None:
        # a count, never the ids: this line reaches builders' logs
        if left_out:
            print(f"left out {len(left_out)} sealed-round board(s); "
                  "scorers add --sealed N", file=sys.stderr)
    if a.cmd == "table":
        holdout = load(man, "holdout")
        sealed = set(sealed_files(man))
        out = Path(a.out)
        out.write_text(table(cache, load(man), holdout, sealed), encoding="utf-8")
        for r in sorted(sealed):
            f = out.parent.parent / "holdout" / f"round-{r}-errata.md"
            f.parent.mkdir(exist_ok=True)
            f.write_text(sealed_table(scache, holdout, r), encoding="utf-8")
        return 0
    if a.cmd == "sweep":
        # unvalidated: a holdout board is fetched and swept before its
        # defects are judged, so before it has its blind verdicts
        raw = _raw(man, "holdout")
        pairs, errored, rows = 0, [], []
        for b in raw:
            if not wanted(b):
                continue
            fetch(where(b), b)
            for script, r in sweep(where(b), b).items():
                rows.append((b["id"], script, r))
                print(b["id"], script, *([f"crashed: {script}"] if "error" in r
                                         else []), json.dumps(r))
                pairs += 1
                if "error" in r:
                    errored.append(f"{b['id']} {script}")
        # a crashed check is a hole in the sweep, not a clean pass (#1189)
        print(f"sweep: {pairs} (board, check) pairs, {len(errored)} errored"
              + (": " + ", ".join(errored) if errored else ""))
        # counts per check, no board names: this reaches builders' logs too
        for line in coverage_line(rows):
            print(line)
        for line in blocker_lines(rows):
            print(line)
        note_left_out()
        return 0
    boards = [b for b in load_all(man) if wanted(b)]
    note_left_out()
    bad = 0
    for b in boards:
        if a.cmd == "fetch":
            fetch(where(b), b)
        else:
            for did, r in run(where(b), b).items():
                print(did, (f"crashed: {r['crashed']} " if r.get("crashed")
                            else "") + json.dumps(r))
                bad += r.get("ran", False) and (
                    bool(r.get("crashed")) or not (r["rev_a"] and not r["rev_b"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
