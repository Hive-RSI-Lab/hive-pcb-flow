"""partnum_backfill.py - give a board's footprints the part numbers the
board's own sources name for them, where the footprint carries none.

A footprint with no LCSC/JLC code and no MPN field is "not judged" by every
check that looks its part up (check_fp_package, check_ratings,
check_bom_sync's package rule, ...), though the same board often names the
part elsewhere: an older KiCad does not copy symbol fields to the board, and
many makers keep the part numbers in a BOM file. This reads them from the
board's own sources, by reference, first found wins:

  1. schematic: the symbol's fields, from --netlist (kicad-cli's export) and,
     for a KiCad 4/5 --sch, its `F n "..." ... "name"` field lines over the
     whole hierarchy;
  2. bom: CSV/TSV/XLSX (first sheet) files whose name says BOM in the board's folder and below
     (not in a folder holding another board), else anywhere in --repo when
     the file name holds the board's stem. A file is read only when it has
     a designator column and a part-number column, and at least 80% of the
     designators it names are footprints on this board (else it is taken to
     be another board's);
  3. parts_json: a parts.json beside the board (as check_fp_package finds
     it), its rows' `refdes`;
  4. other_revision: with --other-pcb, what sources 1-3 of the board's other
     revision (its own schematic, BOM files and parts.json) name for the same
     ref, where that revision's footprint has the same footprint name and
     Value as this one's (else it is counted in `other_revision_unmatched`
     and nothing is borrowed). So two revisions of one board, one shipping a
     BOM and one not, are given part numbers from the same sources, and a
     finding does not look gone or new only because one of them has a BOM.

A field counts as a code when check_fp_package would read it as an LCSC or
JLC one, as an MPN when it would read it as an MPN field. A source naming
two different values for one ref (two BOMs that disagree) gives none.

It only adds: an `LCSC` property where the footprint has no code, an `MPN`
property where it has no MPN (or fills an empty one of that name), hidden,
by text, so the rest of the file stays byte for byte. Nothing else changes.

  partnum_backfill.py --pcb B.kicad_pcb [--src-pcb ORIG] [--repo R]
      [--netlist N.net] [--sch S] [--other-pcb B2 [--other-src-pcb O2]
      [--other-repo R2] [--other-netlist N2] [--other-sch S2]]
      --out C.kicad_pcb --out-report r.json

status: "written" (the copy has at least one new field), "nothing_to_add".
Exit 0 when the report is written, 2 on error.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import bom_cpl  # noqa: E402  (is_lcsc_field)
import check_fp_package as cfp  # noqa: E402  (is_jlc_field, _MPN_FIELD, _CODE)

SCRIPT = "partnum_backfill"
SOURCES = ("schematic", "bom", "parts_json", "other_revision")
MIN_BOM_ON_BOARD = 0.8
MAX_BOM_BYTES = 5_000_000
BLANK = {"", "~", "-", "--", "n/a", "na", "none", "dnp", "dnf", "nc", "tbd",
         "?", "x"}
REF_COLS = {"designator", "designators", "reference", "references", "ref",
            "refs", "refdes", "ref des", "reference designator",
            "reference designators", "part reference"}
TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def unq(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return re.sub(r'\\(.)', r'\1', tok[1:-1])
    return tok


def q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def kind_of(field: str) -> str | None:
    """"code" or "mpn" for a field name the checks read as one, else None."""
    f = field.strip()
    if bom_cpl.is_lcsc_field(f) or cfp.is_jlc_field(f):
        return "code"
    if cfp._MPN_FIELD.match(f):
        return "mpn"
    return None


def clean(kind: str, value) -> str | None:
    v = str(value or "").strip()
    if v.lower() in BLANK:
        return None
    if kind == "code":
        v = v.upper()
        return v if cfp._CODE.match(v) else None
    return v if len(v) <= 80 else None


# ------------------------------------------------------------------ the board

def footprints(text: str) -> list[dict]:
    """[{ref, props: {name: (value, value_start, value_end)}, last_prop_end,
    end}] for each top-level footprint, by a string-aware scan."""
    out = []
    depth = 0
    cur = None
    prop = None          # [name_tok, value_tok_span] while inside a property
    pdepth = 0
    expect_head = False
    for m in TOK.finditer(text):
        tok = m.group()
        if tok == "(":
            depth += 1
            expect_head = True
            continue
        if tok == ")":
            if prop is not None and depth == pdepth:
                if cur is not None and prop[0] is not None:
                    cur["props"][prop[0]] = prop[1] or ("", m.start(), m.start())
                    cur["last_prop_end"] = m.end()
                prop = None
            if cur is not None and depth == 2:
                cur["end"] = m.start()
                out.append(cur)
                cur = None
            depth -= 1
            continue
        if expect_head:
            expect_head = False
            if depth == 2 and tok in ("footprint", "module"):
                cur = {"ref": None, "name": None, "props": {},
                       "last_prop_end": None, "end": None,
                       "head_end": m.end()}
            elif depth == 3 and cur is not None and tok == "property":
                prop, pdepth = [None, None], depth
            continue
        if cur is not None and depth == 2 and cur["name"] is None:
            cur["name"] = unq(tok)
            continue
        if prop is not None and depth == pdepth:
            if prop[0] is None:
                prop[0] = unq(tok)
            elif prop[1] is None:
                prop[1] = (unq(tok), m.start(), m.end())
    for fp in out:
        r = fp["props"].get("Reference")
        fp["ref"] = r[0] if r else None
    return out


def has(fp: dict, kind: str) -> bool:
    return any(kind_of(k) == kind and clean(kind, v[0])
               for k, v in fp["props"].items())


# ---------------------------------------------------------------- the sources

def from_netlist(path: Path | None) -> dict[str, dict[str, set]]:
    out: dict[str, dict[str, set]] = {}
    if not path or not Path(path).is_file():
        return out
    # board_init's s-expression helpers, not parse_netlist: that refuses
    # a netlist whose components lack a footprint, and only fields matter
    import board_init as bi
    data = bi.sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    for section in data[1:]:
        if bi._head(section) != "components":
            continue
        for comp in bi._find(section, "comp"):
            ref = bi._sym(bi._find1(comp, "ref")[1])
            for k, v in bi._comp_fields(comp).items():
                kind = kind_of(k)
                val = clean(kind, v) if kind else None
                if val:
                    out.setdefault(ref, {}).setdefault(kind, set()).add(val)
    return out


def from_legacy_sch(sch: Path | None) -> dict[str, dict[str, set]]:
    out: dict[str, dict[str, set]] = {}
    if not sch or not Path(sch).is_file():
        return out
    import legacy_net
    if not legacy_net.is_legacy(Path(sch)):
        return out
    for f in legacy_net._sheet_files(Path(sch)):
        text = f.read_text(encoding="utf-8", errors="replace")
        for blk in re.findall(r"\$Comp\n(.*?)\$EndComp", text, re.S):
            m = re.search(r"^L (\S+) (\S+)", blk, re.M)
            if not m:
                continue
            refs = {m.group(2), *re.findall(r'^AR .*?Ref="([^"]+)"', blk, re.M)}
            for fm in re.finditer(r'^F\s+(\d+)\s+"((?:[^"\\]|\\.)*)"(.*)$',
                                  blk, re.M):
                if int(fm.group(1)) < 4:
                    continue
                names = re.findall(r'"((?:[^"\\]|\\.)*)"', fm.group(3))
                kind = kind_of(names[-1]) if names else None
                val = clean(kind, fm.group(2)) if kind else None
                if not val:
                    continue
                for r in refs:
                    if r and not r.startswith("#") and "?" not in r:
                        out.setdefault(r, {}).setdefault(kind, set()).add(val)
    return out


def split_refs(cell: str) -> list[str]:
    out = []
    for t in re.split(r"[,;\s]+", cell or ""):
        t = t.strip()
        if not t:
            continue
        m = re.fullmatch(r"([A-Za-z_]+)(\d+)\s*-\s*(?:\1)?(\d+)", t)
        if m and 0 < int(m.group(3)) - int(m.group(2)) <= 200:
            out += [f"{m.group(1)}{i}" for i in
                    range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(t)
    return out


def xlsx_rows(path: Path) -> list[list[str]]:
    """The first sheet of an .xlsx as rows of cell text (stdlib only)."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{ns}t"))
                      for si in root.iter(f"{ns}si")]
        sheets = sorted(n for n in names
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            return []
        first = ("xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml"
                 in sheets else sheets[0])
        root = ET.fromstring(z.read(first))
    rows = []
    for row in root.iter(f"{ns}row"):
        cells: dict[int, str] = {}
        for c in row.iter(f"{ns}c"):
            m = re.match(r"([A-Z]+)", c.get("r") or "")
            col = 0
            for ch in m.group(1) if m else "":
                col = col * 26 + ord(ch) - 64
            col = col - 1 if m else len(cells)
            if c.get("t") == "s":
                v = c.find(f"{ns}v")
                text = shared[int(v.text)] if v is not None and v.text else ""
            elif c.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{ns}t"))
            else:
                v = c.find(f"{ns}v")
                text = v.text or "" if v is not None else ""
            cells[col] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)]
                    if cells else [])
    return rows


def read_bom(path: Path) -> tuple[dict[str, dict[str, set]] | None, str]:
    """(ref -> kind -> values, why) for one BOM file; None when unread."""
    try:
        if path.stat().st_size > MAX_BOM_BYTES:
            return None, "too large"
        if path.suffix.lower() == ".xlsx":
            rows = xlsx_rows(path)
        else:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            head = "\n".join(text.splitlines()[:20])
            delim = max((",", ";", "\t"), key=head.count)
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        return None, f"unreadable: {type(e).__name__}"
    for i, row in enumerate(rows[:20]):
        names = [c.strip().strip("﻿").lower() for c in row]
        rcol = next((j for j, n in enumerate(names) if n in REF_COLS), None)
        if rcol is None:
            continue
        cols = [(j, kind_of(c)) for j, c in enumerate(row) if kind_of(c)]
        if not cols:
            return None, "no part-number column"
        out: dict[str, dict[str, set]] = {}
        for r in rows[i + 1:]:
            if len(r) <= rcol:
                continue
            refs = split_refs(r[rcol])
            for j, kind in cols:
                val = clean(kind, r[j]) if j < len(r) else None
                if val:
                    for ref in refs:
                        out.setdefault(ref, {}).setdefault(kind, set()).add(val)
        return out, "read"
    return None, "no designator column"


def bom_files(src_pcb: Path | None, repo: Path | None) -> list[Path]:
    """The board's folder and below (not a folder holding another board),
    else files in the repo whose name holds the board's stem."""
    if not src_pcb:
        return []
    src_pcb = Path(src_pcb).resolve()

    def boms_in(root: Path, deep: bool) -> list[Path]:
        found = []
        for p in sorted(root.rglob("*") if deep else root.iterdir()):
            if not p.is_file() or ".git" in p.parts:
                continue
            if p.suffix.lower() not in (".csv", ".tsv", ".txt", ".xlsx") \
                    or "bom" not in p.name.lower() or p.name.startswith("~$"):
                continue
            found.append(p)
        return found

    home = src_pcb.parent
    own = []
    for p in boms_in(home, True):
        other = any(x.resolve() != src_pcb
                    for d in p.parents if home in d.parents
                    for x in d.glob("*.kicad_pcb"))
        if not other:
            own.append(p)
    if own or not repo:
        return own
    stem = src_pcb.stem.lower()
    if len(stem) < 3:            # too short to pick a board's files by name
        return []
    return [p for p in boms_in(Path(repo), True) if stem in p.name.lower()]


def from_boms(paths: list[Path], board_refs: set[str],
              repo: Path | None) -> tuple[dict, list, list]:
    merged: dict[str, dict[str, set]] = {}
    used, skipped = [], []
    lower = {r.lower(): r for r in board_refs}
    for p in paths:
        name = str(p.relative_to(repo)) if repo and Path(repo) in p.parents \
            else p.name
        data, why = read_bom(p)
        if data is None:
            skipped.append([name, why])
            continue
        if not data:
            skipped.append([name, "no part numbers"])
            continue
        on = [r for r in data if r in board_refs or r.lower() in lower]
        if len(on) < MIN_BOM_ON_BOARD * len(data):
            skipped.append([name, f"{len(on)} of {len(data)} refs on this board"])
            continue
        used.append(name)
        for r, kinds in data.items():
            ref = r if r in board_refs else lower.get(r.lower())
            if ref is None:
                continue
            for kind, vals in kinds.items():
                merged.setdefault(ref, {}).setdefault(kind, set()).update(vals)
    return merged, used, skipped


def from_parts_json(src_pcb: Path | None) -> dict[str, dict[str, set]]:
    out: dict[str, dict[str, set]] = {}
    pj = cfp.find_parts_json(Path(src_pcb)) if src_pcb else None
    if not pj:
        return out
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    rows = data.get("parts") if isinstance(data, dict) else data
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        for kind, key in (("code", "lcsc"), ("mpn", "mpn")):
            val = clean(kind, r.get(key))
            if val:
                for ref in r.get("refdes") or []:
                    out.setdefault(str(ref), {}).setdefault(kind, set()).add(val)
    return out


# ----------------------------------------------------------------- the plan

def plan(fps: list[dict], sources: dict[str, dict]) -> tuple[list, dict]:
    n = {"footprints": len(fps), "with_code": 0, "with_mpn": 0,
         "with_part_number": 0, "given_part_number": 0,
         "codes_added": 0, "mpns_added": 0, "conflicting": 0,
         "sources_disagree": 0, "without_part_number": 0,
         "other_revision_unmatched": 0,
         "added_from": {s: 0 for s in SOURCES}}
    edits = []
    for fp in fps:
        ref = fp["ref"]
        had = {k: has(fp, k) for k in ("code", "mpn")}
        n["with_code"] += had["code"]
        n["with_mpn"] += had["mpn"]
        n["with_part_number"] += had["code"] or had["mpn"]
        add = {}
        for kind in ("code", "mpn"):
            if had[kind] or not ref:
                continue
            seen = [(s, sources[s].get(ref, {}).get(kind)) for s in SOURCES]
            seen = [(s, v) for s, v in seen if v]
            if not seen:
                continue
            src, vals = seen[0]
            if len(vals) > 1:
                n["conflicting"] += 1
                continue
            val = next(iter(vals))
            if any(val not in v for _, v in seen[1:]):
                n["sources_disagree"] += 1
            add[kind] = (val, src)
        if add:
            edits.append((fp, add))
            n["codes_added"] += "code" in add
            n["mpns_added"] += "mpn" in add
            if not (had["code"] or had["mpn"]):
                n["given_part_number"] += 1
            for _, src in add.values():
                n["added_from"][src] += 1
        elif not (had["code"] or had["mpn"]):
            n["without_part_number"] += 1
    return edits, n


def apply(text: str, edits) -> str:
    ins: list[tuple[int, int, str]] = []
    for fp, add in edits:
        new = []
        for kind, (val, _) in add.items():
            name = "LCSC" if kind == "code" else "MPN"
            old = fp["props"].get(name)
            if old is not None:          # an empty one of that name: fill it
                ins.append((old[1], old[2], q(val)))
                continue
            new.append(f'(property {q(name)} {q(val)} (at 0 0 0) '
                       f'(layer "F.Fab") (hide yes) '
                       f'(effects (font (size 1 1) (thickness 0.15))))')
        if new:
            at = fp["last_prop_end"] or fp["head_end"]
            ins.append((at, at, "".join(" " + s for s in new)))
    for s, e, t in sorted(ins, reverse=True):
        text = text[:s] + t + text[e:]
    return text


def own_sources(fps: list[dict], src: Path, repo: Path | None,
                netlist: str | None, sch: str | None) -> tuple[dict, list, list]:
    """Sources 1-3 for one board: {source: {ref: {kind: set}}}, the BOMs
    read and the BOMs skipped."""
    refs = {fp["ref"] for fp in fps if fp["ref"]}
    sch_f = from_netlist(Path(netlist) if netlist else None)
    for r, kinds in from_legacy_sch(Path(sch) if sch else None).items():
        for k, v in kinds.items():
            sch_f.setdefault(r, {}).setdefault(k, set()).update(v)
    boms, used, skipped = from_boms(bom_files(src, repo), refs, repo)
    return ({"schematic": sch_f, "bom": boms,
             "parts_json": from_parts_json(src)}, used, skipped)


def fp_key(fp: dict) -> tuple[str, str]:
    """What must match for a ref to borrow the other revision's number: the
    footprint name (without its library) and the Value."""
    name = (fp.get("name") or "").split(":")[-1].strip().lower()
    val = fp["props"].get("Value")
    return name, (val[0] if val else "").strip().lower()


def from_other_revision(fps: list[dict], other_fps: list[dict],
                        other: dict) -> tuple[dict, int]:
    """{ref: {kind: {value}}} the other revision's own sources give the same
    ref, first source wins as in plan(), where the two footprints match; and
    how many refs were not borrowed because they did not."""
    mine = {fp["ref"]: fp_key(fp) for fp in fps if fp["ref"]}
    theirs = {fp["ref"]: fp_key(fp) for fp in other_fps if fp["ref"]}
    out: dict[str, dict[str, set]] = {}
    unmatched = 0
    for ref in sorted(mine):
        got = {}
        for kind in ("code", "mpn"):
            for s in SOURCES[:3]:
                vals = other[s].get(ref, {}).get(kind)
                if vals:
                    got[kind] = set(vals)
                    break
        if not got:
            continue
        if theirs.get(ref) != mine[ref]:
            unmatched += 1
            continue
        out[ref] = got
    return out, unmatched


def run(argv=None) -> tuple[dict, int, str | None]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pcb", required=True, help="the board to read")
    ap.add_argument("--src-pcb", help="where the board sits in its checkout "
                    "(for its BOM files and parts.json); default --pcb")
    ap.add_argument("--repo", help="the checkout's root")
    ap.add_argument("--netlist")
    ap.add_argument("--sch")
    ap.add_argument("--other-pcb", help="the board's other revision, as read")
    ap.add_argument("--other-src-pcb", help="where it sits in its checkout")
    ap.add_argument("--other-repo")
    ap.add_argument("--other-netlist")
    ap.add_argument("--other-sch")
    ap.add_argument("--out", required=True)
    ap.add_argument("--out-report")
    a = ap.parse_args(argv)
    pcb = Path(a.pcb)
    src = Path(a.src_pcb or a.pcb)
    repo = Path(a.repo).resolve() if a.repo else None
    text = pcb.read_text(encoding="utf-8")
    fps = footprints(text)
    sources, used, skipped = own_sources(fps, src, repo, a.netlist, a.sch)
    sources["other_revision"], unmatched = {}, 0
    other_rep = None
    if a.other_pcb:
        opcb = Path(a.other_pcb)
        ofps = footprints(opcb.read_text(encoding="utf-8"))
        orepo = Path(a.other_repo).resolve() if a.other_repo else None
        other, oused, _ = own_sources(ofps, Path(a.other_src_pcb or opcb),
                                      orepo, a.other_netlist, a.other_sch)
        sources["other_revision"], unmatched = from_other_revision(
            fps, ofps, other)
        other_rep = {"boms_read": oused,
                     "refs_named": {s: len(v) for s, v in other.items()}}
    edits, n = plan(fps, sources)
    n["other_revision_unmatched"] = unmatched
    rep = {"script": SCRIPT, "counts": n, "boms_read": used,
           "boms_skipped": skipped,
           "refs_named": {s: len(v) for s, v in sources.items()}}
    if other_rep is not None:
        rep["other_revision"] = other_rep
    if edits:
        Path(a.out).write_text(apply(text, edits), encoding="utf-8")
        rep["status"] = "written"
    else:
        rep["status"] = "nothing_to_add"
    return rep, 0, a.out_report


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = (args[args.index("--out-report") + 1]
           if "--out-report" in args[:-1] else None)
    try:
        rep, rc, out = run(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the error JSON contract
        rep, rc = {"script": SCRIPT, "status": "error",
                   "error": f"{type(exc).__name__}: {exc}"}, 2
    text = json.dumps(rep, indent=1)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
