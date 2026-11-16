#!/usr/bin/env python
"""bom_cpl.py - assembly files: the BOM of record, the JLC upload pair, classes.

The P9 assembly-format step (SPEC 6.4). Membership is decided by an
ASSEMBLY CLASS per refdes - never by "whatever the position export happened to
contain" (codex H1). These files come out of one run:

  BOM-full.csv  the BOM OF RECORD. EVERY intended part, whatever its class:
                machine-placed, hand-installed, off-board, do-not-populate,
                customer-supplied, select-on-test, board feature. Columns:
                Comment, Designator, Qty Per Board, Footprint, MPN, LCSC,
                Assembly Class, Instructions. This is the engineering
                deliverable and the thing a human reads.

  BOM.csv       the ASSEMBLER UPLOAD: `smt_placed` and `tht` parts only, in
                JLC's own four columns (Comment, Designator, Footprint, LCSC Part #), ONE
                ROW PER LCSC PART with the comma-joined designator list (a part
                with no LCSC number groups by value + footprint). Two rows with
                the same LCSC number split one part's stock in JLC's BOM
                review. Its designator set is identical to CPL.csv's
                by construction - a DNP site cannot leak into a quote.

  CPL.csv       Designator, Mid X, Mid Y, Layer, Rotation - `smt_placed` and
                `tht` only.
                Mid X/Y in mm from the pos file; Layer Top/Bottom; Rotation =
                (kicad rotation + JLC correction) mod 360 on the top and
                (180 - kicad rotation + JLC correction) mod 360 on the bottom,
                read from below (cpl_verify.py has the derivation; the bottom
                term is from two community exporters plus the geometry, not
                from JLC, so the report's `bottom_preview` asks for a check
                in JLC's order preview). The correction comes
                from the part's OWN LCSC footprint model (cpl_verify.py fits it
                onto the board footprint, pin 1 on pad 1; models cached per
                LCSC number, see --easyeda-cache). Only a part with no model
                falls back to the package table reference/jlc_rotations.csv
                (regex on the footprint name, first match wins - LEARNINGS/S8);
                `rotation_audit[].source` says which one each part used, and
                dfm_check fails the table-fallback parts. A model FETCH that
                failed (HTTP 403/429 rate limit, other HTTP status, network)
                is source `fetch_failed` with the reason in `fetch_error`,
                never `none`/`table` alone: the table rotation is still
                written, but the run is a violation (cpl_model_fetch_failed)
                to rerun once EasyEDA answers. After a 403/429 the rest of
                the run fetches nothing (`easyeda_rate_limited`).

  prebuy.csv    the PRE-BUY LIST: every `smt_placed`/`tht` part parts.json marks
                Extended (`basic: false` or `type: extended`), with its LCSC
                number, qty per board and the build quantity (--build-qty
                boards, default 5). JLC can hold an Extended part as idle stock
                that its BOM review leaves unselected at qty 0 until it is
                bought into your parts inventory; the public parts search cannot
                tell (LEARNINGS 2026-09-24), so every Extended part is listed
                and each row carries that note. Parts are still chosen on stock,
                fit and cost - this list records the pre-buy, it does not avoid
                idle-stock parts. Written (header only) even when empty.

  <ws>_BOM_digikey.csv, <ws>_BOM_mouser.csv (and <ws>_BOM_cost.json when
                distributor API keys are set) - the same BOM of record in each
                distributor's upload format, for a self-assembled build.
                distributor_bom.py writes them; <ws> is the workspace dir name
                (out-dir's parent when out-dir is fab/, else --ws-name / the
                board stem). Quantities are per board x --boards (default 1,
                separate from --build-qty). Missing keys only warn on stderr.

Assembly classes (`assembly_class`, canonical parts.json - NOT a board-local
filter script, which is how rf-de-20m's nine DNP sites used to be handled):

  smt_placed        the assembler places it. In BOM.csv AND CPL.csv.
  tht               through-hole: the assembler fits it in its THT step
                    (wave/selective/hand), not the SMT line. In BOM.csv AND
                    CPL.csv, so it stays in the PCBA order, but never counted
                    as an SMT placement.
  hand_install      bought, fitted by hand after reflow. BOM-full only.
  off_board         bought, mounted off the PCB (heatsink-flanged loads, ...).
  dnp               a real land that must ship EMPTY.
  customer_supplied not part of the PCBA order at all (screws, straps, sinks).
  select_on_test    value/part chosen at the bench; never machine-placed.
  board_feature     realised in copper or board process - nothing to buy
                    (etched air-core spirals, printed shunts).

Where a class comes from, highest priority first:
  1. parts.json line `refdes_class: {"C203": "dnp"}` (or legacy `refdes_dnp`)
  2. parts.json line `assembly_class` (applies to every ref in `refdes`)
  3. the board file's own footprint attributes (`dnp`, `exclude_from_bom`/
     `board_only`, `exclude_from_pos_files`, then `through_hole` -> tht),
     the placement type read off the pads where (attr) contradicts them
     (`attr_mismatch` in the report)
  4. default `smt_placed`
Instructions text: `refdes_notes: {ref: text}` > line `assembly_notes` >
a per-class default sentence.

Stock and part match: every BOM.csv row is checked live against JLC's and
LCSC's own records (stock_check.py, lib/stocklib.py) for --build-qty boards:
a row JLC would not place as written - short, not for sale (its review swaps
in a substitute), unknown to JLC, a code whose model is not the BOM's part, or
a source that did not answer - is a `bom_stock_<status>` violation, so the run
exits 1 and the fab package says which part to change. `stock` in the report
holds every row's verdict, and when --out-dir is a workspace's fab/ the
verdicts go into its ledger.json (lib/ledgerlib.py); --offline (or
HWDE_STOCK_OFFLINE=1) skips it and says so.

bom_cpl GENERATES; polarity/rotation VALIDATION (catching a backwards part) is
dfm_check.py's job (with cpl_verify.py), which also consumes these classes: a missing LCSC on an
`smt_placed`/`tht` part is a release ERROR, on any other class it is not a
finding.

CLI:
  bom_cpl.py --pcb board.kicad_pcb --out-dir fab/ [--pos pos.csv]
             [--parts parts.json] [--rotations jlc_rotations.csv]
             [--name NAME] [--build-qty N] [--ws-name WS] [--boards N]
             [--no-price-lookup] [--easyeda-cache DIR] [--offline]
             [--out report.json]
The model cache is $HWDE_EASYEDA_CACHE, else <parts.json dir>/easyeda (made
on the first fetch), else ~/.cache/hwde/easyeda; --offline (or
HWDE_EASYEDA_OFFLINE=1) never fetches, so an uncached part uses the table.
Exit 0 ok / 1 assembly violations (incomplete BOM, unplaced smt_placed/tht part,
declared-quantity mismatch, a model fetch that failed) / 2 error.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import fpattr  # noqa: E402
import kc  # noqa: E402

REF_ROTATIONS = SCRIPTS.parent / "reference" / "jlc_rotations.csv"

# --------------------------------------------------------- assembly classes

PLACED_CLASS = "smt_placed"
THT_CLASS = "tht"
ASSEMBLY_CLASSES = (
    PLACED_CLASS, THT_CLASS, "hand_install", "off_board", "dnp",
    "customer_supplied", "select_on_test", "board_feature",
)
# The classes the assembler fits from the upload: in BOM.csv and CPL.csv.
ASSEMBLED_CLASSES = (PLACED_CLASS, THT_CLASS)
# Stable presentation order for BOM-full.csv (placed parts first).
CLASS_ORDER = {c: i for i, c in enumerate(ASSEMBLY_CLASSES)}

CLASS_INSTRUCTION = {
    PLACED_CLASS: "",
    THT_CLASS: "Through-hole - JLC THT assembly (or hand-solder after "
               "reflow); not on the SMT line.",
    "hand_install": "Hand-install after reflow - not machine-placed.",
    "off_board": "Mounted OFF the PCB - not machine-placed.",
    "dnp": "DO NOT POPULATE - this land ships empty.",
    "customer_supplied": "Customer-supplied - not part of the PCBA order.",
    "select_on_test": "SELECT ON TEST - measured/chosen at the bench.",
    "board_feature": "Realised in copper - nothing to buy or place.",
}


class AssemblyDataError(ValueError):
    """Malformed canonical assembly data (unknown class, bad shape)."""


# ------------------------------------------------------------- rotation table

def load_rotations(path: Path) -> list[tuple[re.Pattern, float]]:
    """Parse reference/jlc_rotations.csv -> [(compiled regex, degrees)] in file
    order (first match wins). Skips the header and #-comments."""
    rules: list[tuple[re.Pattern, float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("regex,"):
            continue
        # split on the LAST comma (regex may contain commas, rotation cannot)
        idx = line.rfind(",")
        if idx < 0:
            continue
        pat, deg = line[:idx].strip(), line[idx + 1:].strip()
        try:
            rules.append((re.compile(pat), float(deg)))
        except (re.error, ValueError):
            continue
    return rules


def footprint_name(package: str) -> str:
    """The part after a library colon, if any (pos gives the bare name already,
    but board-sourced fpids look like 'LED_SMD:LED_0805_2012Metric')."""
    return package.split(":", 1)[1] if ":" in package else package


def correct_rotation(package: str, base_deg: float,
                     rules: list[tuple[re.Pattern, float]]
                     ) -> tuple[float, float, str | None]:
    """(final_rotation, correction_applied, matched_pattern|None).
    final = (base + correction) mod 360. `base_deg` is already the side's
    base (cpl_verify.cpl_rotation(side, kicad_rot, 0))."""
    name = footprint_name(package)
    for pat, deg in rules:
        if pat.search(name):
            final = (base_deg + deg) % 360.0
            return (final, deg, pat.pattern)
    return (base_deg % 360.0, 0.0, None)


# ------------------------------------------------------------------- pos read

def parse_pos_csv(text: str) -> list[dict]:
    """kicad-cli pos CSV -> [{ref,val,package,x,y,rot,side}]. Header:
    Ref,Val,Package,PosX,PosY,Rot,Side."""
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        try:
            rows.append({
                "ref": r["Ref"].strip(),
                "val": r["Val"].strip(),
                "package": r["Package"].strip(),
                "x": float(r["PosX"]),
                "y": float(r["PosY"]),
                "rot": float(r["Rot"]),
                "side": r["Side"].strip().lower(),
            })
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unexpected pos-CSV row {r}: {exc}") from exc
    return rows


def _get_pos(pcb: Path, pos: Path | None, out_dir: Path, name: str) -> str:
    if pos is not None:
        return Path(pos).read_text(encoding="utf-8")
    cli = kc.resolve_cli()
    pos_file = out_dir / f"{name}-pos.csv"
    res = kc.export_pos(cli, pcb, pos_file, side="both", fmt="csv", units="mm")
    if res.get("status") != "pass":
        raise RuntimeError(f"pos export failed: {res.get('stderr') or res}")
    return pos_file.read_text(encoding="utf-8")


# ------------------------------------------------------------------ parts map

# easyeda2kicad (lib_pull) names the field `LCSC Part`; hand-made and older
# libraries say `LCSC`, JLC's own templates `LCSC Part #` / `LCSC#`.
_LCSC_FIELD_RE = re.compile(r"^LCSC(?:\s*PART)?(?:\s*(?:#|NO\.?|NUMBER))?$", re.I)


def is_lcsc_field(name: str) -> bool:
    return bool(_LCSC_FIELD_RE.match(name.strip()))


def board_part_fields(pcb: Path) -> dict[str, dict]:
    """ref -> {lcsc, mpn, dnp, attr_class} read off the BOARD's own footprints.

    board_init copies custom symbol fields (LCSC included) onto footprints
    since S14, so a pipeline board carries its own ref->LCSC mapping - the
    file being fabbed is the closest-to-truth source. parts.json (per-ref
    shapes) remains an explicit override; the S6 per-DISTINCT-part shape has
    no refs and cannot map by itself.

    The footprint `(attr ...)` flags are KiCad's own class statement and are
    read as one: `dnp` -> class dnp, `exclude_from_bom`/`board_only` -> class
    board_feature (mounting holes, fiducials, heatsink lands - board_init marks
    them so, LEARNINGS/S8), `exclude_from_pos_files` alone -> hand_install (in
    the BOM, not machine-placed), and `through_hole` with none of those ->
    tht (in the upload, fitted in the THT step). parts.json outranks all of them; see
    resolve_assembly.

    The placement type is read off the pads when the two disagree
    (fpattr.judge): EasyEDA's XR0850 inductor is `through_hole` on two SMD
    pads (lite-main L1-L4), and is classed by its pads, SMT. Such a ref
    carries `attr_mismatch` {attr, pads_say, detail}; the report lists them
    and check_fp_attr fails the board until the footprint is fixed.
    """
    import sexpdata
    out: dict[str, dict] = {}
    try:
        data = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    except Exception:
        return out

    def head(n):
        return n[0].value() if isinstance(n, list) and n \
            and isinstance(n[0], sexpdata.Symbol) else None

    def sval(v):
        return v.value() if isinstance(v, sexpdata.Symbol) else str(v)

    for node in data[1:]:
        if head(node) != "footprint":
            continue
        ref = lcsc = None
        attrs: set[str] = set()
        angle = 0.0
        for sub in node[1:]:
            if head(sub) == "property" and len(sub) >= 3:
                pname = sval(sub[1])
                if pname == "Reference":
                    ref = sval(sub[2])
                elif is_lcsc_field(pname):
                    lcsc = sval(sub[2]).strip() or lcsc
            elif head(sub) == "attr":
                attrs |= {sval(a) for a in sub[1:]}
            elif head(sub) == "at" and len(sub) >= 4:
                angle = float(sub[3])
        # the pads, not the (attr) type, say how the part is fitted
        verdict = fpattr.judge(node, -angle)
        mismatch = None
        if verdict["mismatch"]:
            mismatch = {"attr": verdict["declared"],
                        "pads_say": verdict["pads_say"],
                        "detail": fpattr.why(verdict)}
            if verdict["pads_say"]:
                attrs = (attrs - set(fpattr.TYPES)) | {verdict["pads_say"]}
        if "dnp" in attrs:
            attr_class = "dnp"
        elif {"exclude_from_bom", "board_only"} & attrs:
            attr_class = "board_feature"
        elif "exclude_from_pos_files" in attrs:
            attr_class = "hand_install"
        elif "through_hole" in attrs:
            attr_class = THT_CLASS
        else:
            attr_class = None
        if ref and (lcsc or attr_class or mismatch):
            out[ref] = {"lcsc": lcsc, "mpn": None,
                        "dnp": "dnp" in attrs, "attr_class": attr_class}
            if mismatch:
                out[ref]["attr_mismatch"] = mismatch
    return out


def board_lcsc_map(pcb: Path) -> dict[str, dict]:
    """ref -> {lcsc} for refs the board itself carries an LCSC field for."""
    return {r: f for r, f in board_part_fields(pcb).items() if f.get("lcsc")}


def _iter_part_lines(data):
    """The three tolerated parts.json shapes -> an iterable of line dicts (or
    (ref, lcsc) pairs for the bare mapping shape)."""
    items = data.get("parts", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        for ref, v in items.items():
            yield ({"refdes": [ref], **v} if isinstance(v, dict)
                   else {"refdes": [ref], "lcsc": v})
    elif isinstance(items, list):
        for ent in items:
            if isinstance(ent, dict):
                yield ent


def _line_refs(ent: dict) -> list[str]:
    refs = ent.get("refdes") or ent.get("refs") or []
    if not refs and ent.get("ref"):
        refs = [ent["ref"]]
    return [str(r).strip() for r in refs if str(r).strip()]


def load_parts_map(path: Path | None) -> dict[str, dict]:
    """ref -> {lcsc, mpn?}. Tolerant of several parts.json shapes:
      {"parts":[{"ref":"U1","lcsc":"C123","mpn":".."}, ...]}
      [{"refs":["R1","R2"],"lcsc":"C1"} , ...]
      {"U1":"C123", "R1":{"lcsc":"C1"}}
    """
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for ent in _iter_part_lines(data):
        lcsc = ent.get("lcsc") or ent.get("LCSC")
        mpn = ent.get("mpn") or ent.get("mfr_part")
        for ref in _line_refs(ent):
            if lcsc:
                out[ref] = {"lcsc": str(lcsc), "mpn": mpn}
    return out


def _check_class(value, where: str) -> str:
    cls = str(value).strip()
    if cls not in ASSEMBLY_CLASSES:
        raise AssemblyDataError(
            f"{where}: unknown assembly_class {value!r} - allowed: "
            + ", ".join(ASSEMBLY_CLASSES))
    return cls


def jlc_status(ent: dict) -> str:
    """basic | extended | unknown from a parts.json line (`basic` bool, else
    `type`; JLC's "preferred" parts carry no extended fee, so count as basic)."""
    if ent.get("basic") is not None:
        return "basic" if ent["basic"] else "extended"
    t = str(ent.get("type", "")).strip().lower()
    if t in ("basic", "preferred", "extended"):
        return "extended" if t == "extended" else "basic"
    return "unknown"


def load_parts_records(path: Path | None) -> list[dict]:
    """parts.json -> canonical assembly records, one per part LINE.

    {refs, lcsc, mpn, jlc_status, value, package, qty_per_board, qty_populated,
     cls (line default | None), refdes_class {ref: cls},
     notes (line default | None), refdes_notes {ref: text}}
    """
    if path is None:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs: list[dict] = []
    for i, ent in enumerate(_iter_part_lines(data)):
        refs = _line_refs(ent)
        lcsc = ent.get("lcsc") or ent.get("LCSC") or ""
        where = f"parts line {i} ({lcsc or ent.get('mpn') or refs or '?'})"

        cls = None
        if ent.get("assembly_class"):
            cls = _check_class(ent["assembly_class"], where)

        per_ref: dict[str, str] = {}
        raw = ent.get("refdes_class") or {}
        if not isinstance(raw, dict):
            raise AssemblyDataError(f"{where}: refdes_class must be an object")
        for ref, val in raw.items():
            per_ref[str(ref)] = _check_class(val, f"{where} ref {ref}")
        for ref in (ent.get("refdes_dnp") or []):   # legacy sugar for `dnp`
            per_ref.setdefault(str(ref), "dnp")

        notes_map = ent.get("refdes_notes") or {}
        if not isinstance(notes_map, dict):
            raise AssemblyDataError(f"{where}: refdes_notes must be an object")

        recs.append({
            "refs": refs,
            "lcsc": str(lcsc),
            "distributor": ent.get("distributor") or "",
            "distributor_pn": ent.get("distributor_pn") or "",
            "mpn": ent.get("mpn") or ent.get("mfr_part") or "",
            "jlc_status": jlc_status(ent),
            "value": ent.get("value") or "",
            "package": ent.get("package") or "",
            "qty_per_board": ent.get("qty_per_board"),
            "qty_populated": ent.get("qty_per_board_populated"),
            "cls": cls,
            "refdes_class": per_ref,
            "notes": ent.get("assembly_notes") or None,
            "refdes_notes": {str(k): str(v) for k, v in notes_map.items()},
        })
    return recs


def bind_refs_by_lcsc(records: list[dict], board_fields: dict[str, dict]
                      ) -> list[dict]:
    """Give each refdes-less line (the P3 per-distinct-part shape) the board
    refs whose footprint carries its LCSC number, so its MPN reaches the BOM
    of record. Only lines that leave the class at the default: a refdes-less
    line with its own class is a separate off-board item (see build_bom_full).
    A ref another line names keeps that line."""
    named = {r for rec in records for r in rec["refs"]}
    for rec in records:
        if rec["refs"] or not rec["lcsc"] or \
                (rec["cls"] and rec["cls"] not in ASSEMBLED_CLASSES):
            continue
        rec["refs"] = sorted((r for r, f in board_fields.items()
                              if f.get("lcsc") == rec["lcsc"]
                              and r not in named), key=_natural_key)
    return records


def resolve_assembly(pos_refs: set[str], records: list[dict],
                     board_fields: dict[str, dict]
                     ) -> tuple[dict[str, str], dict[str, str],
                                dict[str, str]]:
    """-> (ref -> class, ref -> instructions, ref -> where the class came from).

    Covers every ref in the position export plus every ref named in the
    canonical parts data, so an off-board or customer-supplied part that has no
    placement still reaches the BOM of record."""
    cls: dict[str, str] = {}
    src: dict[str, str] = {}
    note: dict[str, str] = {}

    for ref in pos_refs:
        cls[ref], src[ref] = PLACED_CLASS, "default"
    # 3. the board's own footprint attributes
    for ref, f in board_fields.items():
        if f.get("attr_class"):
            cls[ref], src[ref] = f["attr_class"], "board_attr"
    # 2. the parts line default
    for rec in records:
        for ref in rec["refs"]:
            if rec["cls"]:
                cls[ref], src[ref] = rec["cls"], "parts_line"
            else:
                cls.setdefault(ref, PLACED_CLASS)
                src.setdefault(ref, "default")
            if rec["notes"]:
                note[ref] = rec["notes"]
    # 1. the per-ref override
    for rec in records:
        for ref, c in rec["refdes_class"].items():
            cls[ref], src[ref] = c, "parts_ref"
        for ref, txt in rec["refdes_notes"].items():
            note[ref] = txt

    for ref, c in cls.items():
        if not note.get(ref):
            note[ref] = CLASS_INSTRUCTION[c]
    return cls, note, src


# --------------------------------------------------------------- BOM / CPL

def _natural_key(ref: str):
    m = re.match(r"([A-Za-z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def build_bom(parts: list[dict], parts_map: dict[str, dict]) -> list[dict]:
    """One JLC upload row per LCSC part (value + footprint when a part has no
    LCSC number); Comment and Footprint come from the first designator.

    `parts` must already be the placed set (see run()); this function does not
    decide membership."""
    groups: dict[tuple, list[dict]] = {}
    for p in parts:
        lcsc = parts_map.get(p["ref"], {}).get("lcsc", "")
        key = ("lcsc", lcsc) if lcsc else ("value", p["val"], p["package"])
        groups.setdefault(key, []).append(p)
    rows = []
    for key, members in groups.items():
        members.sort(key=lambda q: _natural_key(q["ref"]))
        rows.append({
            "Comment": members[0]["val"],
            "Designator": ",".join(q["ref"] for q in members),
            "Footprint": members[0]["package"],
            "LCSC": key[1] if key[0] == "lcsc" else "",
        })
    rows.sort(key=lambda r: _natural_key(r["Designator"].split(",")[0]))
    return rows


BOM_FULL_FIELDS = ["Comment", "Designator", "Qty Per Board", "Footprint",
                   "MPN", "LCSC", "Assembly Class", "Instructions"]


def build_bom_full(parts: list[dict], records: list[dict],
                   classes: dict[str, str], notes: dict[str, str],
                   parts_map: dict[str, dict]) -> list[dict]:
    """The BOM of record: every classified refdes, plus refdes-less lines that
    declare a non-placed class (screws, straps, a heatsink)."""
    pos_by_ref = {p["ref"]: p for p in parts}
    rec_by_ref: dict[str, dict] = {}
    for rec in records:
        for ref in rec["refs"]:
            rec_by_ref.setdefault(ref, rec)

    groups: dict[tuple, list[str]] = {}
    for ref in sorted(classes, key=_natural_key):
        pos = pos_by_ref.get(ref)
        rec = rec_by_ref.get(ref, {})
        comment = pos["val"] if pos else (rec.get("value") or "")
        pkg = pos["package"] if pos else (rec.get("package") or "")
        key = (comment, pkg, rec.get("mpn", ""),
               parts_map.get(ref, {}).get("lcsc", "") or rec.get("lcsc", ""),
               classes[ref], notes.get(ref, ""))
        groups.setdefault(key, []).append(ref)

    rows = []
    for key, refs in groups.items():
        comment, pkg, mpn, lcsc, cls, note = key
        refs_sorted = sorted(refs, key=_natural_key)
        rows.append({
            "Comment": comment,
            "Designator": ",".join(refs_sorted),
            "Qty Per Board": str(len(refs_sorted)),
            "Footprint": pkg,
            "MPN": mpn,
            "LCSC": lcsc,
            "Assembly Class": cls,
            "Instructions": note,
        })

    # Lines with no refdes at all are only carried when they explicitly declare
    # a non-placed class - otherwise they are the S6 per-distinct-part shape,
    # already covered through the position export.
    for rec in records:
        if rec["refs"] or not rec["cls"] or rec["cls"] in ASSEMBLED_CLASSES:
            continue
        rows.append({
            "Comment": rec["value"],
            "Designator": "",
            "Qty Per Board": str(rec["qty_per_board"] or 1),
            "Footprint": rec["package"],
            "MPN": rec["mpn"],
            "LCSC": rec["lcsc"],
            "Assembly Class": rec["cls"],
            "Instructions": rec["notes"] or CLASS_INSTRUCTION[rec["cls"]],
        })

    rows.sort(key=lambda r: (CLASS_ORDER[r["Assembly Class"]],
                             _natural_key(r["Designator"].split(",")[0])))
    return rows


DEFAULT_BUILD_QTY = 5   # JLC's smallest PCBA order, and order_quote's first qty
PREBUY_FIELDS = ["LCSC", "MPN", "Comment", "Designator", "Qty Per Board",
                 "Boards", "Qty To Buy", "Note"]
PREBUY_NOTE = ("Extended part: JLC's BOM review may show it as idle stock "
               "(unselected, qty 0) that has to be bought into your parts "
               "inventory before assembly.")


def build_prebuy(bom_rows: list[dict], records: list[dict],
                 build_qty: int) -> list[dict]:
    """Pre-buy rows: each placed BOM row whose part parts.json marks Extended.
    Qty To Buy = designators on the row x build_qty boards.

    Owner, 2026-09-24: "each Extended part with its LCSC number and the build
    quantity" - Basic and unknown-status parts are left off."""
    status = {r["lcsc"]: r["jlc_status"] for r in records if r["lcsc"]}
    mpn = {r["lcsc"]: r["mpn"] for r in records if r["lcsc"]}
    rows = []
    for b in bom_rows:
        if not b["LCSC"] or status.get(b["LCSC"]) != "extended":
            continue
        per_board = len(b["Designator"].split(","))
        rows.append({
            "LCSC": b["LCSC"], "MPN": mpn.get(b["LCSC"], ""),
            "Comment": b["Comment"], "Designator": b["Designator"],
            "Qty Per Board": str(per_board), "Boards": str(build_qty),
            "Qty To Buy": str(per_board * build_qty), "Note": PREBUY_NOTE,
        })
    return rows


def build_cpl(parts: list[dict], rules, derive=None
              ) -> tuple[list[dict], list[dict]]:
    """CPL rows + a parallel rotation-correction audit trail. `derive(ref)`
    gives the rotation the part's own LCSC model needs, or None - then the
    package table decides. A `derive.errors[ref]` (the model fetch failed)
    makes the source `fetch_failed`, whatever the table did. `correction` in
    the audit is on the side's base: kicad rotation on the top, 180 - it on the
    bottom."""
    from cpl_verify import cpl_rotation
    cpl, audit = [], []
    fetch_errors = getattr(derive, "errors", {})
    for p in sorted(parts, key=lambda q: _natural_key(q["ref"])):
        model_rot = derive(p["ref"]) if derive else None
        fetch_error = None
        bottom = p["side"] in ("bottom", "back")
        base = cpl_rotation("bottom" if bottom else "top", p["rot"], 0.0)
        if model_rot is None:
            final, corr, pat = correct_rotation(p["package"], base, rules)
            source = "table" if pat else "none"
            fetch_error = fetch_errors.get(p["ref"])
            if fetch_error:
                source = "fetch_failed"
        else:
            final = model_rot % 360.0
            corr = round((final - base) % 360.0, 4)
            pat, source = None, "lcsc_model"
        layer = "Bottom" if bottom else "Top"
        # the part's centre where the footprint's origin isn't it (from the
        # LCSC model; the pos file's y is up, the board's down)
        off = derive.offset(p["ref"], final) if model_rot is not None and hasattr(derive, "offset") else None
        mid_x, mid_y = p["x"], p["y"]
        if off:
            mid_x, mid_y = mid_x + off[0], mid_y - off[1]
        cpl.append({
            "Designator": p["ref"],
            "Mid X": f'{mid_x:.4f}',
            "Mid Y": f'{mid_y:.4f}',
            "Layer": layer,
            "Rotation": f"{final:.4f}",
        })
        audit.append({"ref": p["ref"], "package": p["package"],
                      "base_rot": round(p["rot"], 4),
                      "correction": corr, "final_rot": round(final, 4),
                      "matched": pat, "source": source, "layer": layer})
        if off:
            audit[-1]["offset_mm"] = [round(off[0], 4), round(off[1], 4)]
        if fetch_error:
            audit[-1]["fetch_error"] = fetch_error
    return cpl, audit


def model_deriver(pcb: Path, parts_map: dict[str, dict], cache_dir: Path,
                  fetch: bool):
    """ref -> the CPL rotation the part's LCSC model needs, or None. A model
    fetch that failed leaves its reason in `derive.errors[ref]`."""
    import cpl_verify
    import easyeda
    # A run from a pos file alone (no board) has nothing to fit onto.
    fps = cpl_verify.board_footprints(pcb) if Path(pcb).is_file() else {}

    def derive(ref: str):
        fp, lcsc = fps.get(ref), parts_map.get(ref, {}).get("lcsc")
        if fp is None or not lcsc:
            return None
        # offline, an uncached part uses the table: only a fetch that was
        # tried and failed is a fetch failure here
        errs: dict = {}
        model = easyeda.get(lcsc, cache_dir, fetch=fetch, pace_s=1.0,
                            errors=errs if fetch else None)
        if errs:
            derive.errors[ref] = next(iter(errs.values()))
        return cpl_verify.derive_rotation(fp, model)

    def offset(ref: str, cpl_rot: float):
        fp, lcsc = fps.get(ref), parts_map.get(ref, {}).get("lcsc")
        if fp is None or not lcsc:
            return None
        return cpl_verify.placement_offset(fp, easyeda.get(lcsc, cache_dir, fetch=False), cpl_rot)
    derive.errors = {}
    derive.offset = offset
    return derive


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def check_declared_quantities(records: list[dict],
                              classes: dict[str, str]) -> list[dict]:
    """`qty_per_board_populated` (and `qty_per_board`) on a parts line are the
    owner's declaration of how many sites ship fitted. Derive the same number
    from the classes and refuse to disagree silently - this is the guard the
    board-local rf-de filter used to carry."""
    out = []
    for rec in records:
        if not rec["refs"]:
            continue
        placed = [r for r in rec["refs"]
                  if classes.get(r, PLACED_CLASS) in ASSEMBLED_CLASSES]
        want = rec["qty_populated"]
        if want is None and rec["qty_per_board"] is not None \
                and len(placed) == len(rec["refs"]):
            want = rec["qty_per_board"]
        if want is None:
            continue
        if int(want) != len(placed):
            out.append({"lcsc": rec["lcsc"], "mpn": rec["mpn"],
                        "declared_populated": int(want),
                        "derived_populated": len(placed),
                        "refs": list(rec["refs"])})
    return out


def run(pcb: Path, out_dir: Path, pos: Path | None = None,
        parts_json: Path | None = None, rotations: Path | None = None,
        name: str | None = None,
        build_qty: int = DEFAULT_BUILD_QTY, ws_name: str | None = None,
        boards: int = 1, lookup_prices: bool = False,
        transport=None, easyeda_cache: Path | None = None,
        fetch_models: bool | None = None, stock: bool | None = None,
        stock_fetch=None) -> dict:
    if build_qty < 1:
        raise ValueError(f"build_qty must be >= 1, got {build_qty}")
    name = name or pcb.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = load_rotations(rotations or REF_ROTATIONS)
    parts = parse_pos_csv(_get_pos(pcb, pos, out_dir, name))
    board_fields = board_part_fields(pcb)
    records = bind_refs_by_lcsc(load_parts_records(parts_json), board_fields)
    # board footprint LCSC fields first; per-ref parts.json entries override
    parts_map = {**{r: f for r, f in board_fields.items() if f.get("lcsc")},
                 **load_parts_map(parts_json)}

    pos_refs = {p["ref"] for p in parts}
    classes, notes, sources = resolve_assembly(pos_refs, records, board_fields)

    placed = [p for p in parts if classes[p["ref"]] in ASSEMBLED_CLASSES]
    # Classed `smt_placed` or `tht` but never exported a position: either the
    # part is missing from the board or its class is wrong. Both are release
    # defects, so this is a violation rather than a silently shorter BOM.
    unplaced_smt = sorted((r for r, c in classes.items()
                           if c in ASSEMBLED_CLASSES and r not in pos_refs),
                          key=_natural_key)
    not_placed = [{"ref": r, "class": classes[r], "source": sources[r]}
                  for r in sorted(pos_refs - {p["ref"] for p in placed},
                                  key=_natural_key)]

    bom_rows = build_bom(placed, parts_map)
    bom_full_rows = build_bom_full(parts, records, classes, notes, parts_map)
    import cpl_verify
    cache = easyeda_cache or cpl_verify.default_cache(parts_json)
    fetch = (not cpl_verify.offline()) if fetch_models is None else fetch_models
    cpl_rows, audit = build_cpl(
        placed, rules, model_deriver(pcb, parts_map, cache, fetch))
    prebuy_rows = build_prebuy(bom_rows, records, build_qty)
    qty_mismatch = check_declared_quantities(records, classes)

    bom_path = out_dir / "BOM.csv"
    bom_full_path = out_dir / "BOM-full.csv"
    cpl_path = out_dir / "CPL.csv"
    prebuy_path = out_dir / "prebuy.csv"
    # JLC's own BOM template names the part column "LCSC Part #"; the
    # in-memory rows keep the "LCSC" key every other consumer reads.
    _write_csv(bom_path, ["Comment", "Designator", "Footprint", "LCSC Part #"],
               [{"Comment": r["Comment"], "Designator": r["Designator"],
                 "Footprint": r["Footprint"], "LCSC Part #": r["LCSC"]}
                for r in bom_rows])
    _write_csv(bom_full_path, BOM_FULL_FIELDS, bom_full_rows)
    _write_csv(cpl_path, ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"],
               cpl_rows)
    _write_csv(prebuy_path, PREBUY_FIELDS, prebuy_rows)
    # Distributor BOMs for a self-assembled build. Prices are looked up only
    # when the caller asks (the CLI does; dfm_check's scratch run does not).
    import distributor_bom
    dist = distributor_bom.write(
        bom_full_rows, out_dir,
        ws_name or distributor_bom.default_ws_name(out_dir, name),
        parts_dir=Path(parts_json).parent if parts_json
        else out_dir.resolve().parent / "parts",
        boards=boards, lookup_prices=lookup_prices, transport=transport)

    # A BOM line is COMPLETE when the part can actually be bought: an LCSC
    # number, or a manufacturer part number with a named distributor line. A
    # part nobody places needs neither - an off-board load, a customer's screw
    # and an empty DNP land are all complete entries with nothing at all
    # (codex H1). Not being on LCSC is a separate, weaker fact: JLC cannot
    # source it, which only matters if the board is a PCBA build.
    alt_sourced = set()
    for rec in records:
        if rec["mpn"] and (rec["distributor_pn"] or rec["distributor"]):
            alt_sourced |= set(rec["refs"])

    def _no_lcsc(refs):
        return sorted((r for r in refs
                       if not parts_map.get(r, {}).get("lcsc")),
                      key=_natural_key)

    missing = _no_lcsc(p["ref"] for p in placed)
    unsourced = [r for r in missing if r not in alt_sourced]
    off_lcsc = [r for r in missing if r in alt_sourced]
    missing_unplaced = _no_lcsc(r for r, c in classes.items()
                                if c not in ASSEMBLED_CLASSES)

    class_counts: dict[str, int] = {}
    for c in classes.values():
        class_counts[c] = class_counts.get(c, 0) + 1

    violations = []
    if unsourced:
        violations.append({
            "kind": "bom_unsourced", "refs": unsourced,
            "message": f"{len(unsourced)} machine-placed part(s) have neither "
                       f"an LCSC number nor a distributor line"})
    if unplaced_smt:
        violations.append({
            "kind": "assembly_unplaced_smt", "refs": unplaced_smt,
            "message": f"{len(unplaced_smt)} part(s) are classed "
                       f"{' or '.join(ASSEMBLED_CLASSES)} but have no "
                       f"placement"})
    for m in qty_mismatch:
        violations.append({
            "kind": "assembly_qty_mismatch", "refs": m["refs"],
            "message": f"{m['lcsc'] or m['mpn']}: parts.json declares "
                       f"{m['declared_populated']} populated, classes give "
                       f"{m['derived_populated']}"})

    fetch_failed = [a["ref"] for a in audit if a["source"] == "fetch_failed"]
    import easyeda
    rate_limited = easyeda.rate_limited()
    if fetch_failed:
        why = rate_limited or "; ".join(sorted(
            {a["fetch_error"] for a in audit if a.get("fetch_error")}))
        violations.append({
            "kind": "cpl_model_fetch_failed", "refs": fetch_failed,
            "message": f"could not fetch the LCSC model for "
                       f"{len(fetch_failed)} part(s) ({why}); their CPL "
                       f"rotation fell back to the package table and is not "
                       f"a model result - rerun once EasyEDA answers "
                       f"(a rate limit clears in minutes) to fill {cache}"})

    # Can JLC place every row today (stock_check.py)? Live unless asked not
    # to (--offline, HWDE_STOCK_OFFLINE, dfm_check's scratch run): a skipped
    # check says so in the report, and the board's ledger keeps its stock
    # facts unknown, so the order step still sees the gap.
    import stock_check
    import stocklib
    if stock is None:
        stock = not stocklib.offline()
    if stock:
        stock_rep = stock_check.check_rows(
            bom_rows, boards=build_qty,
            mpns=stock_check.mpn_index(Path(parts_json).parent if parts_json
                                       else out_dir.resolve().parent
                                       / "parts"),
            fetch=stock_fetch)
        violations += stock_check.violations(stock_rep)
        import ledgerlib
        ws = out_dir.resolve().parent
        if (ws / "state.json").is_file():
            led = ledgerlib.record_stock(ws, stock_rep)
            stock_rep["ledger"] = str(led) if led else None
    else:
        stock_rep = {"status": "skipped",
                     "why": "not checked: stock and part match are unknown "
                            "until stock_check.py runs live"}

    attr_mismatch = [{"ref": r, "class": classes.get(r),
                      **board_fields[r]["attr_mismatch"]}
                     for r in sorted(board_fields, key=_natural_key)
                     if board_fields[r].get("attr_mismatch")]

    corrected = [a for a in audit if a["correction"] != 0.0]
    below = [a["ref"] for a in audit if a["layer"] == "Bottom"]
    bottom_preview = ({"refs": below, "note": cpl_verify.BOTTOM_PREVIEW_NOTE}
                      if below else None)
    import castellation
    # None = not known (no board file beside a supplied pos file)
    has_board = Path(pcb).is_file()
    return {
        "script": "bom_cpl",
        "status": "violations" if violations else "pass",
        "board": pcb.name,
        "n_parts": len(parts),
        "n_placed": len(placed),
        "bom": str(bom_path),
        "bom_full": str(bom_full_path),
        "cpl": str(cpl_path),
        "prebuy": str(prebuy_path),
        "build_qty": build_qty,
        "distributor_boms": dist,
        "stock": stock_rep,
        "bom_rows": bom_rows,
        "prebuy_rows": prebuy_rows,
        "bom_full_rows": bom_full_rows,
        "cpl_rows": cpl_rows,
        "assembly_classes": {r: classes[r] for r in sorted(classes,
                                                           key=_natural_key)},
        "class_counts": class_counts,
        "not_placed": not_placed,
        "rotation_audit": audit,
        "bottom_preview": bottom_preview,
        "n_rotation_corrections": len(corrected),
        "rotation_from_table": [a["ref"] for a in audit
                                if a["source"] != "lcsc_model"],
        "easyeda_cache": str(cache),
        "model_fetch_failed": fetch_failed,
        "easyeda_rate_limited": rate_limited,
        "missing_lcsc": missing,
        "missing_lcsc_unplaced": missing_unplaced,
        "unsourced": unsourced,
        "off_lcsc": off_lcsc,
        "unplaced_smt": unplaced_smt,
        "qty_mismatch": qty_mismatch,
        # footprints whose (attr) type the pads contradict: classed by the
        # pads above; check_fp_attr fails the board until they're fixed
        "attr_mismatch": attr_mismatch,
        "violations": violations,
        "bom_complete": not unsourced,
        # counted from the board's pads, so the report and the order say
        # "castellated" only when the board really has castellated pads
        "castellated_pads": castellation.count(pcb) if has_board else None,
        "castellated_refs": (sorted(castellation.refs(pcb), key=_natural_key)
                             if has_board else None),
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True, help="input .kicad_pcb")
    ap.add_argument("--out-dir", required=True, help="fab output directory")
    ap.add_argument("--pos", help="pre-exported pos CSV (else export via kc.py)")
    ap.add_argument("--parts", help="parts.json ref->LCSC map (BOM-of-record)")
    ap.add_argument("--rotations", help="override reference/jlc_rotations.csv")
    ap.add_argument("--name", help="basename (default: board stem)")
    ap.add_argument("--build-qty", type=int, default=DEFAULT_BUILD_QTY,
                    help="boards in the build, for the pre-buy list "
                         f"(default {DEFAULT_BUILD_QTY})")
    ap.add_argument("--ws-name", help="prefix of the distributor BOMs "
                    "(default: the workspace dir when --out-dir is its fab/)")
    ap.add_argument("--boards", type=int, default=1,
                    help="boards to buy for in the distributor BOMs (default 1)")
    ap.add_argument("--no-price-lookup", action="store_true",
                    help="skip the DigiKey/Mouser APIs even with keys set")
    ap.add_argument("--easyeda-cache", help="LCSC footprint model cache "
                    "(default: env.easyeda_cache)")
    ap.add_argument("--offline", action="store_true",
                    help="never fetch LCSC models (uncached parts use the "
                         "table) and skip the live stock check")
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args(argv)
    if args.boards < 1:
        ap.error(f"--boards must be >= 1, got {args.boards}")

    try:
        rep = run(Path(args.pcb), Path(args.out_dir),
                  pos=Path(args.pos) if args.pos else None,
                  parts_json=Path(args.parts) if args.parts else None,
                  rotations=Path(args.rotations) if args.rotations else None,
                  name=args.name, build_qty=args.build_qty,
                  ws_name=args.ws_name, boards=args.boards,
                  lookup_prices=not args.no_price_lookup,
                  easyeda_cache=Path(args.easyeda_cache)
                  if args.easyeda_cache else None,
                  fetch_models=False if args.offline else None,
                  stock=False if args.offline else None)
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        err = {"script": "bom_cpl", "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(err, indent=1)
        (Path(args.out).write_text(text, encoding="utf-8") if args.out
         else print(text))
        return 2

    for w in rep["distributor_boms"]["warnings"]:
        print(f"bom_cpl: {w}", file=sys.stderr)
    for b in rep["stock"].get("blocking") or []:
        print(f"bom_cpl: JLC cannot place {','.join(b['refs'])} "
              f"{b['lcsc']} as ordered [{b['status']}]: {b['why']}",
              file=sys.stderr)
    for m in rep["attr_mismatch"]:
        print(f"bom_cpl: {m['ref']} classed {m['class']} by its pads: "
              f"{m['detail']} (fix the footprint: pcb-flow fpfix --lib, rule G)",
              file=sys.stderr)
    text = json.dumps(rep, indent=1)
    (Path(args.out).write_text(text, encoding="utf-8") if args.out
     else print(text))
    return 0 if rep["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
