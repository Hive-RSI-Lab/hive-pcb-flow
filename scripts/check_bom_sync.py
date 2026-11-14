"""check_bom_sync.py - DNP, BOM, CPL and board attributes agree (P8/P9).

One concern: the assembly files say something different from the design.
The real hits: lumina-par and rf-de-20m shipped nine DNP sites populated in
BOM and CPL (DFA-07); rf-term-150w left R1, the 250 W load and the board's
whole function, out of both (DFA-04); rf-de-20m and g0-sense passed the dfm
gate with the BOM leg silently skipped because parts.json was absent (DFA-08).

Which assembly class each ref has is decided exactly as bom_cpl.py decides it
(its resolve_assembly over parts.json, the board's footprint attrs and the
smt_placed default, imported read-only), so this check and the generator can
never disagree about the rules; the check only reports where the FILES now
disagree with the design. The schematic's own `(dnp yes)` is read as a fourth
statement bom_cpl does not consult.

Rules, each a finding's `kind` (error unless marked):
 - bom_dnp_populated: a ref the schematic, the board's `dnp` attr or parts.json
   marks DNP is listed in BOM.csv or CPL.csv - it would be placed. Only
   files named so are bom_cpl's and held to it; another maker's BOM or
   pos file (a KiCad pos lists DNP parts unless told not to) is judged on
   unknown and missing refs only, and the DNP refs it lists are named in
   `not_judged`.
 - bom_dnp_mismatch: the schematic says DNP but the class bom_cpl resolves is
   still smt_placed or tht (board attr and parts.json both silent), or the board's
   `dnp` attr is set while the schematic says `(dnp no)`. The next bom_cpl run
   would place a part the schematic leaves empty, or the reverse.
 - bom_ref_missing: a board footprint whose class is smt_placed or tht is absent from
   the BOM or the CPL, or a ref of any class but board_feature is absent from
   BOM-full.csv (the BOM of record).
 - bom_ref_unknown: the BOM, the CPL or BOM-full.csv names a designator that
   is no footprint on the board. A designator matches whatever its case, as
   at JLC (the board's boot1 is the CPL's BOOT1), unless two footprints
   differ only by case.
 - bom_package_mismatch: the part a ref is bought as (its LCSC number, from
   the board's footprint field, parts.json or the BOM - each is checked) is
   a chip of another size than the footprint's (an 0402 resistor on an 0603
   pad: oasis v0.2 Rfbb1, which JLC's BOM review caught and hwde did not,
   docs/errata-corpus.md). The part's package comes from the workspace's part
   data (parts.json's `package`, parts/<LCSC>.json), then the pinned table
   of JLC's records (reference/lcsc_packages.json, lcsc_packages.py: how a
   foreign board with no part data is judged offline, as the score and CI
   run), and, with --live, from JLC's own record (lib/stocklib.py); a part
   with none is named in `skipped`, never passed. Only chip sizes compare (engcalc.package_code:
   0201..2512 on both sides); a SOT or QFN part is not this rule's.
 - bom_pos_excluded (warning): an SMD footprint carries
   `exclude_from_pos_files` (and neither `exclude_from_bom` nor
   `board_only`) and parts.json gives it no class, so it became hand_install
   from the attr alone and will not be machine-placed.

A missing input is not a finding, because a bare board (the golden corpus)
has no fab files and that is no fault of the design. Instead the report's
`legs` says which legs ran, and `skipped` names each one that did not and why
(DFA-08: a skipped leg is stated, never left silent).

Inputs, each found beside a pipeline workspace's board when not given:
  schematic   <pcb stem>.kicad_sch beside the board, its sheets followed
  fab dir     --fab-dir, else the board's dir when it holds BOM.csv or
              CPL.csv, else <ws>/fab
  fab files   in the fab dir, each role by its own name first (BOM.csv,
              CPL.csv, BOM-full.csv), else the one file in the fab dir or
              a folder directly in it that fits the role, by order_submit's
              patterns in any case: the BOM is *bom*.csv (BOM-full.csv
              aside), the CPL *cpl*.csv, else *-pos.csv (a -top-/-bottom-pos
              pair is read as one). Where order_submit takes the first of
              two files that fit one role, this leaves the role unread and
              says so; --bom and --cpl name the files outright. The
              designators are read from a Designator, Designators, Ref,
              Reference or RefDes column, split on commas, spaces or
              semicolons, a range (C1-C4) read as each ref in it. The
              report's `fab_files` names what each role read.
  parts.json  --parts (a parts dir or the file), else <ws>/parts/parts.json

CLI: --pcb board.kicad_pcb [--fab-dir DIR] [--bom FILE] [--cpl FILE...]
     [--parts DIR|parts.json] [--live] [--out report.json]
     exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

import sexpdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import bom_cpl  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import lcsc_packages  # noqa: E402
import stocklib  # noqa: E402
from checklib import violation  # noqa: E402

SCRIPT = "check_bom_sync"
PLACED = bom_cpl.PLACED_CLASS
ASSEMBLED = bom_cpl.ASSEMBLED_CLASSES
FAB_FILES = ("BOM.csv", "CPL.csv", "BOM-full.csv")
# the role each of bom_cpl's own files plays; any other file is a foreign one
ROLES = {"BOM": "BOM.csv", "CPL": "CPL.csv", "BOM-full": "BOM-full.csv"}
LCSC_COLS = ("LCSC Part #", "LCSC", "JLCPCB Part #", "LCSC Part")
# the designator column, whatever its case: vendor.py's names for it (KiCad's
# pos says Ref, a distributor BOM Designators) bar its loose "parts"
REF_COLS = ("designator", "designators", "reference", "references", "refdes",
            "ref des", "ref", "refs", "part/designator")
# a KiCad pos export split by side, read as one CPL
POS_PAIRS = (("-top-pos.csv", "-bottom-pos.csv"),
             ("-front-pos.csv", "-back-pos.csv"))
# a designator range in a grouped BOM: C1-C4
_RANGE = re.compile(r"([A-Za-z_]+)(\d+)-([A-Za-z_]+)(\d+)")


def _head(n):
    return n[0].value() if isinstance(n, list) and n \
        and isinstance(n[0], sexpdata.Symbol) else None


def _sval(v) -> str:
    return v.value() if isinstance(v, sexpdata.Symbol) else str(v)


def _child(node, name):
    for sub in node[1:]:
        if _head(sub) == name:
            return sub
    return None


def board_footprints(pcb: Path) -> dict[str, dict]:
    """ref -> {pos, attrs, smd} for every footprint on the board."""
    data = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for node in data[1:]:
        if _head(node) != "footprint":
            continue
        ref, pos, attrs = None, None, set()
        fpid = _sval(node[1]) if len(node) > 1 \
            and not isinstance(node[1], list) else ""
        for sub in node[1:]:
            h = _head(sub)
            if h == "property" and len(sub) >= 3 \
                    and _sval(sub[1]) == "Reference":
                ref = _sval(sub[2])
            elif h == "at" and len(sub) >= 3:
                pos = (float(sub[1]), float(sub[2]))
            elif h == "attr":
                attrs |= {_sval(a) for a in sub[1:]}
        if ref and not ref.startswith("#"):
            out[ref] = {"pos": pos, "attrs": attrs, "smd": "smd" in attrs,
                        "fpid": fpid}
    return out


def schematic_dnp(sch: Path) -> dict[str, bool]:
    """ref -> the schematic's `(dnp yes|no)`, over the root sheet and every
    sheet it names (each file read once). Refs come from the symbol's
    instances block (the per-sheet-instance truth), else its Reference."""
    out: dict[str, bool] = {}
    seen: set[Path] = set()
    todo = [sch]
    while todo:
        f = todo.pop()
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        data = sexpdata.loads(f.read_text(encoding="utf-8"))
        for node in data[1:]:
            h = _head(node)
            if h == "sheet":
                for sub in node[1:]:
                    if _head(sub) == "property" and len(sub) >= 3 \
                            and _sval(sub[1]) == "Sheetfile":
                        todo.append(f.parent / _sval(sub[2]))
            if h != "symbol" or _child(node, "lib_id") is None:
                continue
            d = _child(node, "dnp")
            dnp = d is not None and len(d) > 1 and _sval(d[1]) == "yes"
            refs = []
            inst = _child(node, "instances")
            for proj in (inst[1:] if inst else []):
                for path in proj[1:]:
                    r = _child(path, "reference") \
                        if isinstance(path, list) else None
                    if r is not None and len(r) > 1:
                        refs.append(_sval(r[1]))
            if not refs:
                for sub in node[1:]:
                    if _head(sub) == "property" and len(sub) >= 3 \
                            and _sval(sub[1]) == "Reference":
                        refs.append(_sval(sub[2]))
            for r in refs:
                if not r.startswith("#"):
                    out[r] = dnp
    return out


def _csv(path: Path) -> tuple[str | None, list[dict]]:
    """(its designator column or None, its rows) for a fab csv."""
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    names = {str(f).strip().lower(): f for f in reader.fieldnames or () if f}
    col = next((names[c] for c in REF_COLS if c in names), None)
    return col, list(reader)


def _split_refs(cell) -> list[str]:
    """A BOM row's designators, joined by commas, spaces or semicolons. A
    range (C1-C4, as kicad-cli's grouped BOM writes them) is each ref in
    it."""
    out: list[str] = []
    for tok in re.split(r"[,;\s]+", str(cell or "")):
        m = _RANGE.fullmatch(tok)
        lo, hi = (int(m[2]), int(m[4])) if m else (0, 0)
        if m and m[1] == m[3] and m[2] == str(lo) and m[4] == str(hi) \
                and lo < hi <= lo + 10000:
            out += [f"{m[1]}{n}" for n in range(lo, hi + 1)]
        elif tok:
            out.append(tok)
    return out


def csv_designators(path: Path) -> set[str] | None:
    """Every designator a BOM/CPL csv names, None when it has no designator
    column (REF_COLS) to read them from."""
    col, rows = _csv(path)
    if col is None:
        return None
    return {r for row in rows for r in _split_refs(row.get(col))}


def csv_lcsc(path: Path) -> dict[str, str]:
    """ref -> the LCSC number a BOM csv buys it as (rows with one)."""
    col, rows = _csv(path)
    out: dict[str, str] = {}
    for row in rows if col else ():
        code = next((str(row[c]).strip() for c in LCSC_COLS
                     if row.get(c)), "")
        for r in _split_refs(row.get(col)) if code else ():
            out[r] = code
    return out


def part_packages(records: list[dict], parts: Path | None) -> dict:
    """LCSC -> {package, source} from the workspace's own part data:
    parts/<LCSC>.json, then parts.json lines over it."""
    out: dict[str, dict] = {}
    if parts is not None and parts.parent.is_dir():
        for f in sorted(parts.parent.glob("C*.json")):
            try:
                ent = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(ent, dict) and ent.get("package"):
                code = str(ent.get("lcsc") or f.stem).strip()
                out[code] = {"package": str(ent["package"]),
                             "source": f"parts/{f.name}"}
    for rec in records:
        if rec["lcsc"] and rec["package"]:
            out[rec["lcsc"]] = {"package": str(rec["package"]),
                                "source": "parts.json"}
    return out


def table_package(code: str, table: dict) -> dict | None:
    """{package, model, source} from the pinned table, None if absent."""
    ent = table.get(code)
    if not isinstance(ent, dict):
        return None
    return {"package": str(ent.get("package") or ""),
            "model": ent.get("model") or None,
            "source": "JLC's record of "
                      f"{ent.get('read') or 'an earlier date'}, "
                      "reference/lcsc_packages.json"}


def jlc_package(code: str, fetch=None) -> dict | None:
    """{package, model, source} from JLC's record, None if JLC has none.
    stocklib.SourceError when JLC can't be asked."""
    rec = stocklib.jlc_record(code, fetch or stocklib.http_fetch)
    if rec is None:
        return None
    return {"package": rec.get("package") or "", "model": rec.get("model"),
            "source": "JLC's record"}


def find_fab_dir(pcb: Path, given: str | None) -> Path | None:
    if given:
        return Path(given)
    if any((pcb.parent / f).is_file() for f in FAB_FILES[:2]):
        return pcb.parent
    ws_fab = pcb.parent.parent / "fab"
    return ws_fab if ws_fab.is_dir() else None


def _fab_csvs(fab_dir: Path) -> list[Path]:
    """The csv files in fab_dir and in each folder directly inside it."""
    out: list[Path] = []
    dirs = [fab_dir] + sorted(
        d for d in fab_dir.iterdir() if d.is_dir()
        and not d.name.startswith(".") and d.name != "__pycache__")
    for d in dirs:
        out += sorted(p for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() == ".csv")
    return out


def _pos_pair(hits: list[Path]) -> list[Path] | None:
    """A KiCad pos export split by side ([top, bottom]), else None."""
    if len(hits) != 2 or hits[0].parent != hits[1].parent:
        return None
    for top, bottom in POS_PAIRS:
        t = [p for p in hits if p.name.lower().endswith(top)]
        b = [p for p in hits if p.name.lower().endswith(bottom)]
        if len(t) == 1 and len(b) == 1 and \
                t[0].name.lower()[:-len(top)] == \
                b[0].name.lower()[:-len(bottom)]:
            return [t[0], b[0]]
    return None


def fab_files(fab_dir: Path | None, bom=None, cpl=None) \
        -> tuple[dict[str, list[Path]], list[str]]:
    """role (BOM, CPL, BOM-full) -> the files it is read from, and a line
    for each role two or more files could fill. --bom/--cpl first, then the
    role's own name in fab_dir, then the one file in fab_dir or a folder
    directly in it that fits (order_submit.collect_package's patterns, in
    any case)."""
    files: dict[str, list[Path]] = {}
    notes: list[str] = []
    for role, given in (("BOM", bom), ("CPL", cpl)):
        if given:
            paths = [Path(g) for g in
                     ([given] if isinstance(given, (str, Path)) else given)]
            for p in paths:
                if not p.is_file():
                    raise checklib.CheckError(
                        f"--{role.lower()} file not found: {p}")
            files[role] = paths
    if fab_dir is None or not fab_dir.is_dir():
        return files, notes
    csvs = _fab_csvs(fab_dir)

    def low(p):
        return p.name.lower()
    fits = {
        "BOM": [[p for p in csvs if "bom" in low(p)
                 and low(p) != "bom-full.csv"]],
        "CPL": [[p for p in csvs if "cpl" in low(p)],
                [p for p in csvs if low(p).endswith("-pos.csv")]],
        "BOM-full": [[p for p in csvs if low(p) == "bom-full.csv"]]}
    for role, name in ROLES.items():
        if role in files:
            continue
        if (fab_dir / name).is_file():
            files[role] = [fab_dir / name]
            continue
        for hits in fits[role]:
            if len(hits) == 1:
                files[role] = hits
            elif role == "CPL" and _pos_pair(hits):
                files[role] = _pos_pair(hits)
            elif hits:
                flag = {"BOM": " (--bom names the one to read)",
                        "CPL": " (--cpl names the one to read)"}
                notes.append(
                    f"fab: {len(hits)} files could be the {role} ("
                    + ", ".join(_rel(p, fab_dir) for p in hits)
                    + f"), so the {role} leg did not run"
                    + flag.get(role, ""))
            if hits:
                break
    return files, notes


def _rel(p: Path, base: Path | None) -> str:
    """p as the report names it: from the fab dir when it is in it."""
    try:
        return str(p.relative_to(base)) if base is not None else str(p)
    except ValueError:
        return str(p)


def find_parts(pcb: Path, given: str | None) -> Path | None:
    if given:
        p = Path(given)
        return p / "parts.json" if p.is_dir() else p
    for p in (pcb.parent.parent / "parts" / "parts.json",
              pcb.parent / "parts" / "parts.json"):
        if p.is_file():
            return p
    return None


def check(pcb: Path, fab_dir: Path | None, parts: Path | None,
          live: bool = False, fetch=None, table: dict | None = None,
          bom=None, cpl=None) -> tuple:
    fps = board_footprints(pcb)
    sch_path = pcb.with_suffix(".kicad_sch")
    sch = schematic_dnp(sch_path) if sch_path.is_file() else {}
    have_parts = parts is not None and parts.is_file()
    records = bom_cpl.load_parts_records(parts if have_parts else None)
    classes, _, src = bom_cpl.resolve_assembly(
        set(fps), records, bom_cpl.board_part_fields(pcb))
    parts_refs = {r for rec in records for r in rec["refs"]} | {
        r for rec in records for r in rec["refdes_class"]}

    viols: list[dict] = []

    def add(code, sev, ref, msg):
        f = fps.get(ref)
        viols.append(violation(SCRIPT, sev, f["pos"] if f else None,
                               None, None, [ref], msg, SCRIPT, kind=code,
                               ref=ref))

    def is_dnp(ref) -> bool:
        return classes.get(ref) == "dnp" or sch.get(ref, False)

    # DFA-07: the schematic's DNP is a machine-readable statement that bom_cpl
    # does not read, so it must agree with the class bom_cpl resolves.
    for ref in sorted(fps, key=bom_cpl._natural_key):
        if ref not in sch:
            continue
        cls = classes.get(ref, PLACED)
        if sch[ref] and cls in ASSEMBLED:
            add("bom_dnp_mismatch", "error", ref,
                f"{ref} is DNP in the schematic but bom_cpl resolves it "
                f"{cls} ({src.get(ref, 'default')}); set the footprint's "
                "dnp attr or a parts.json refdes_class")
        elif not sch[ref] and "dnp" in fps[ref]["attrs"] \
                and src.get(ref) == "board_attr":
            add("bom_dnp_mismatch", "error", ref,
                f"{ref} has the board dnp attr but the schematic says "
                "(dnp no)")
    for ref in sorted(fps, key=bom_cpl._natural_key):
        f = fps[ref]
        # board_feature (exclude_from_bom / board_only: fiducials, printed
        # coils, pogo pads) is no part, so only an attr-made hand_install is
        if f["smd"] and classes.get(ref) == "hand_install" \
                and "exclude_from_pos_files" in f["attrs"] \
                and ref not in parts_refs and not is_dnp(ref):
            add("bom_pos_excluded", "warning", ref,
                f"{ref} is SMD with exclude_from_pos_files and no parts.json "
                "class, so it became hand_install from the attr alone")

    # JLC matches a designator whatever its case (oasis's board has boot1,
    # the CPL it shipped BOOT1), so a fab file's ref takes the board's
    # spelling when exactly one footprint folds to it.
    folded: dict[str, list[str]] = {}
    for ref in fps:
        folded.setdefault(ref.casefold(), []).append(ref)

    def board_ref(ref: str) -> str:
        if ref in fps:
            return ref
        hit = folded.get(ref.casefold(), [])
        return hit[0] if len(hit) == 1 else ref

    legs = {"schematic": bool(sch), "parts": have_parts, "fab": False}
    found, fab_notes = fab_files(fab_dir, bom, cpl)
    # role -> (label, refs listed, strict): bom_cpl's own BOM.csv and CPL.csv
    # are held to its DNP rule; another maker's file only to the refs
    files: dict[str, tuple[str, set[str], bool]] = {}
    for role, paths in found.items():
        label = " + ".join(_rel(p, fab_dir) for p in paths)
        refs: set[str] = set()
        for p in paths:
            try:
                got = csv_designators(p)
            except (OSError, UnicodeDecodeError, csv.Error) as exc:
                got, why = None, f"could not be read ({exc})"
            else:
                why = "has no designator column (" + ", ".join(
                    c.title() for c in REF_COLS[:5]) + ")"
            if got is None:
                fab_notes.append(f"fab: {_rel(p, fab_dir)} {why}, so the "
                                 f"{role} leg did not run")
                break
            refs |= {board_ref(r) for r in got}
        else:
            files[role] = (label, refs, all(p.name == ROLES[role]
                                            for p in paths))
    legs["fab"] = bool(files)
    legs["files"] = {role: [_rel(p, fab_dir) for p in found[role]]
                     for role in files}

    # The part each ref is bought as against its pad's chip size. Every
    # source that names an LCSC number for a ref is a statement of its own.
    bought: dict[tuple[str, str], list[str]] = {}
    for ref, f in bom_cpl.board_part_fields(pcb).items():
        if f.get("lcsc"):
            bought.setdefault((ref, f["lcsc"]), []).append("the board")
    for rec in records:
        for ref in rec["refs"] if rec["lcsc"] else ():
            bought.setdefault((ref, rec["lcsc"]), []).append("parts.json")
    if "BOM" in files:
        for p in found["BOM"]:
            for ref, code in csv_lcsc(p).items():
                bought.setdefault((board_ref(ref), code), []).append(
                    _rel(p, fab_dir))
    known = part_packages(records, parts if have_parts else None)
    table = lcsc_packages.load() if table is None else table
    unknown: set[str] = set()
    compared = 0
    for (ref, code), where in sorted(bought.items(), key=lambda kv: (
            bom_cpl._natural_key(kv[0][0]), kv[0][1])):
        fp = fps.get(ref)
        if fp is None or is_dnp(ref):
            continue
        pad = engcalc.package_code(fp["fpid"])
        if pad is None:
            continue
        if code not in known and code in table:
            known[code] = table_package(code, table)
        if code not in known and live:
            try:
                known[code] = jlc_package(code, fetch)
            except stocklib.SourceError as exc:
                known[code] = None
                unknown.add(f"{code} ({exc})")
        pkg = known.get(code)
        size = engcalc.package_code(pkg["package"]) if pkg else None
        if size is None:
            if not pkg:
                unknown.add(code)
            continue
        compared += 1
        if size != pad:
            what = f"{code} ({pkg['model']})" if pkg.get("model") else code
            add("bom_package_mismatch", "error", ref,
                f"{ref} is bought as {what}, a {size} part ({pkg['source']}), "
                f"but its footprint {fp['fpid']} is {pad}: the part won't fit "
                f"the pads (LCSC number from {', '.join(where)})")
    legs["packages"] = compared
    legs["skipped"] = [m for m, missing in (
        ("fab: no BOM or CPL file found (BOM.csv, CPL.csv, BOM-full.csv, "
         "*bom*.csv, *cpl*.csv or *-pos.csv, in the fab dir or a folder in "
         "it), so the BOM and CPL legs did not run",
         not files and not fab_notes),
        ("parts: no parts.json found, so classes come from board attrs and "
         "the schematic only", not have_parts),
        ("schematic: no schematic DNP marks read", not sch),
        (f"packages: no package on record for {', '.join(sorted(unknown))}"
         + ("" if live else "; --live asks JLC"), unknown)) if missing]
    legs["skipped"][:0] = fab_notes

    for role in ("BOM", "CPL"):
        if role not in files:
            continue
        name, listed, strict = files[role]
        dnp_listed = []
        for ref in sorted(listed, key=bom_cpl._natural_key):
            if ref not in fps:
                add("bom_ref_unknown", "error", ref,
                    f"{name} lists {ref}, which is no footprint on the board")
            elif is_dnp(ref) and strict:
                add("bom_dnp_populated", "error", ref,
                    f"{ref} is DNP but {name} lists it, so it would be placed")
            elif is_dnp(ref):
                dnp_listed.append(ref)
        for ref in sorted(fps, key=bom_cpl._natural_key):
            if classes.get(ref) in ASSEMBLED and not is_dnp(ref) \
                    and ref not in listed:
                add("bom_ref_missing", "error", ref,
                    f"{ref} is {classes[ref]} but missing from {name}")
        if dnp_listed:
            legs["skipped"].append(
                f"fab: {name} lists DNP {', '.join(dnp_listed)}; only "
                "bom_cpl's BOM.csv and CPL.csv are judged on DNP, so see "
                "that the assembler leaves them off")
    if "BOM-full" in files:
        name, full, _ = files["BOM-full"]
        for ref in sorted(full - set(fps), key=bom_cpl._natural_key):
            add("bom_ref_unknown", "error", ref,
                f"{name} lists {ref}, which is no footprint on the board")
        for ref in sorted(fps, key=bom_cpl._natural_key):
            if classes.get(ref) != "board_feature" and ref not in full:
                add("bom_ref_missing", "error", ref,
                    f"{ref} ({classes.get(ref, PLACED)}) is missing from "
                    f"{name}, the BOM of record")
    return viols, legs


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--fab-dir", help="dir holding BOM.csv / CPL.csv / "
                    "BOM-full.csv (default: beside the board, else <ws>/fab)")
    ap.add_argument("--bom", help="the BOM csv to judge (default: the fab "
                    "dir's BOM.csv, else its one *bom*.csv)")
    ap.add_argument("--cpl", nargs="+", metavar="FILE",
                    help="the CPL or pos csv(s) to judge, read as one "
                    "(default: the fab dir's CPL.csv, else its one *cpl*.csv "
                    "or *-pos.csv)")
    ap.add_argument("--parts", help="parts dir or parts.json (default "
                    "<ws>/parts/parts.json)")
    ap.add_argument("--live", action="store_true",
                    help="ask JLC for the package of a part the workspace's "
                    "part data gives none")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    fab = find_fab_dir(pcb, args.fab_dir)
    parts = find_parts(pcb, args.parts)
    viols, legs = check(pcb, fab, parts, live=args.live, bom=args.bom,
                        cpl=args.cpl)
    skipped = legs.pop("skipped")
    fab_files_read = legs.pop("files")
    return checklib.report(
        SCRIPT, pcb, viols, legs=legs, skipped=skipped,
        fab_dir=str(fab) if fab else None, fab_files=fab_files_read,
        parts=str(parts) if parts else None,
        not_judged=skipped,
        nothing_to_judge=None if any(legs.values()) else
        "no schematic, parts.json or fab files to compare"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
