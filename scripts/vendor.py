#!/usr/bin/env python
"""vendor.py - fab and assembly vendors beyond JLCPCB (docs/vendors.md).

Each vendor is a profile in reference/vendors/<id>.yaml (lib/vendors.py):
its fab minimums in jlc_capabilities.yaml's shape, what it builds, how it
assembles (sides, pitch, sourcing, BOM and CPL columns, rotation), the files
it takes, its published prices and lead times, each with its source and date.

  vendor.py list
      every vendor: services, layers, how it's priced, when its numbers were
      copied.
  vendor.py show VENDOR
      one profile, resolved (JLC's tables included).
  vendor.py fit --pcb board.kicad_pcb [--fab-dir fab] [--vendors a,b|all]
                [--assembly] [--qty 5,10] [--no-dfm] [--out fit.json]
      can each vendor build this board? Board-level limits (services,
      layers, size, copper, thickness, castellations, blind/buried vias;
      with --assembly: bottom-side parts, finest pitch, board size for
      assembly) from the board itself, then the gerber-level dfm_check
      families (copper, drill, silk, castellation) against each vendor's
      own minimums - on --fab-dir's gerbers, else one export made here -
      and an estimated price where the vendor publishes one. Writes a
      `vendor` fact per vendor into the board's ledger. Exit 0 when at
      least one vendor fits / 1 none does / 2 error.
  vendor.py package --workspace WS --vendor VENDOR [--out-dir DIR]
      the vendor's upload set from the workspace's fab/ (gerber zip,
      BOM-full.csv, <board>-pos.csv): gerbers.zip, BOM.csv and CPL.csv in
      the vendor's columns and rotation, and ORDER.md (options to pick,
      what is still unchecked). Default out: WS/fab/<vendor>/.
  vendor.py choose --workspace WS --vendor VENDOR
      record the board's vendor in state.json; the ledger then expects its
      `vendor` fact (a passing fit on the current board) before release.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shutil
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import checklib  # noqa: E402
import ledgerlib  # noqa: E402
import safelib  # noqa: E402
import vendors  # noqa: E402

SCRIPT = "vendor"
# the dfm families that measure the board against a fab's minimums; the rest
# (zone fill, placement, polarity, bom) don't change with the vendor
DFM_SKIP = ("zone_fill", "placement")
ASSEMBLED = ("smt_placed", "tht")
# 1.55 and 1.6 mm are one build (the nominal differs by fab, not the board)
THICK_TOL_MM = 0.06


# ------------------------------------------------------------ board facts

def board_facts(pcb: Path) -> dict:
    """What a vendor's limits are judged on, read from the board itself."""
    import castellation
    import dfm_check
    import geom
    bg = geom.load_board(pcb)
    if bg.outline is None or bg.outline.is_empty:
        raise checklib.CheckError(f"{pcb.name} has no board outline")
    x0, y0, x1, y1 = bg.outline.bounds
    oz, oz_src = dfm_check.derive_copper_oz(pcb)
    cu = bg.copper_layers
    ends = {cu[0], cu[-1]}
    blind = sorted({v.layers[0] + "-" + v.layers[-1] for v in bg.vias_of()
                    if v.layers and set((v.layers[0], v.layers[-1])) != ends})
    bottom: set[str] = set()
    pads_by_ref: dict[str, list] = {}
    for p in bg.pads_of():
        if p.drill is None:
            pads_by_ref.setdefault(p.ref, []).append(p)
            if p.layers and all(lay == "B.Cu" for lay in p.layers):
                bottom.add(p.ref)
    pitch, pitch_ref = None, None
    for ref, pads in pads_by_ref.items():
        for i, a in enumerate(pads):
            for b in pads[i + 1:]:
                # lead pitch: neighbours of one row are pads of one size (a
                # thermal pad or a mounting tab is not a lead)
                if (round(a.size[0], 2), round(a.size[1], 2)) != \
                        (round(b.size[0], 2), round(b.size[1], 2)):
                    continue
                d = math.dist(a.center, b.center)
                if d > 1e-3 and (pitch is None or d < pitch):
                    pitch, pitch_ref = d, ref
    thick = bg.stackup.total_thickness or bg.thickness
    no_mpn = _refs_without_mpn(pcb)
    return {"layers": len(cu), "copper_oz": oz, "copper_oz_source": oz_src,
            "width_mm": round(x1 - x0, 2), "height_mm": round(y1 - y0, 2),
            "thickness_mm": round(float(thick), 3),
            "castellated_pads": castellation.count(pcb),
            "blind_buried_vias": blind,
            "tented_vias": _tented_vias(pcb, cu),
            "bottom_smd_refs": sorted(bottom),
            "min_pitch_mm": round(pitch, 3) if pitch else None,
            "min_pitch_ref": pitch_ref,
            "refs_without_mpn": no_mpn}


def _tented_vias(pcb: Path, cu: list[str]) -> int:
    """Vias with mask over them on an outer side they reach. Most boards state
    no tenting at all and get KiCad's default, which is tented."""
    import check_wicking
    import sexpdata
    root = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    vias = check_wicking.read_vias(root, check_wicking.board_defaults(root), cu)
    return sum(1 for v in vias if any(v["tent"][s] for s in v["sides"]))


_FP = re.compile(r"\(footprint\s")
# KiCad 8 (property "Reference" "U1"), KiCad 6 (fp_text reference "U1") (#1429)
_REF = re.compile(r'\((?:property\s+"Reference"|fp_text\s+reference)\s+"?([^"\s)]*)')
_MPN = re.compile(r'\(property\s+"(MPN|Manufacturer[ _]Part[ _]Number|'
                  r'Mfr[ _]Part[ _]?#?|MFN|Part[ _]Number)"\s+"([^"]+)"', re.I)


def _refs_without_mpn(pcb: Path) -> list[str]:
    """Footprints with pads whose board-file fields name no MPN: what a
    vendor that reads its BOM from the board file itself (assembly.upload:
    native, e.g. Aisler) can't source."""
    text = Path(pcb).read_text(encoding="utf-8", errors="replace")
    out = []
    for block in _FP.split(text)[1:]:
        m = _REF.search(block)
        if not m or "(pad " not in block or "exclude_from_bom" in block \
                or m.group(1).startswith(("#", "H", "MH", "TP", "FID")):
            continue
        if not _MPN.search(block):
            out.append(m.group(1))
    return sorted(set(out))


def _fits_size(w: float, h: float, lim, larger: bool) -> bool:
    a, b = sorted((float(lim[0]), float(lim[1])))
    s, t = sorted((w, h))
    return (s <= a and t <= b) if larger else (s >= a and t >= b)


def _row(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def limits(prof: dict, bf: dict, assembly: bool) -> list[dict]:
    """The board against the vendor's published limits: pass / fail /
    unknown (the vendor doesn't publish that limit)."""
    caps = prof["capabilities"]
    board = caps["board"]
    meta = prof["meta"]
    svc = meta.get("services") or {}
    rows = []
    if not svc.get("pcb", True):
        rows.append(_row("service_pcb", "fail", "makes no bare boards"))
    lay = board.get("layers")
    n = bf["layers"]
    rows.append(_row("layers", "unknown" if lay is None else
                     "pass" if n in lay else "fail",
                     f"{n} layers; offered: {lay or 'not published'}"))
    w, h = bf["width_mm"], bf["height_mm"]
    for key, larger in (("max_size_mm", True), ("min_size_mm", False)):
        lim = board.get(key)
        if lim:
            ok = _fits_size(w, h, lim, larger)
            rows.append(_row(key[:3] + "_size", "pass" if ok else "fail",
                             f"{w} x {h} mm; {key[:3]} {lim[0]} x {lim[1]}"))
        else:
            rows.append(_row(key[:3] + "_size", "unknown",
                             f"{w} x {h} mm; {key} not published"))
    key = vendors.rule_key(n, bf["copper_oz"])
    rows.append(_row("rules", "pass" if key in caps["design_rules"]
                     else "fail",
                     f"{key}: " + ("rules on file" if key in
                                   caps["design_rules"] else
                                   "no published minimums for this build "
                                   f"(have {', '.join(caps['design_rules'])})")))
    opts = board.get("thickness_options_mm")
    t = bf["thickness_mm"]
    if opts:
        ok = any(abs(t - float(o)) <= THICK_TOL_MM for o in opts)
        rows.append(_row("thickness", "pass" if ok else "fail",
                         f"{t} mm; offered {opts}"))
    else:
        rows.append(_row("thickness", "unknown",
                         f"{t} mm; thicknesses not published"))
    if bf["castellated_pads"]:
        c = board.get("castellated")
        rows.append(_row("castellated", "unknown" if c is None else
                         "pass" if c else "fail",
                         f"{bf['castellated_pads']} castellated pads; "
                         f"offered: {c if c is not None else 'not published'}"))
    if bf["blind_buried_vias"]:
        c = board.get("blind_buried_vias")
        rows.append(_row("blind_buried_vias", "unknown" if c is None else
                         "pass" if c else "fail",
                         f"vias spanning {bf['blind_buried_vias']}"))
    if bf["tented_vias"]:
        c = board.get("via_tenting")
        n = f"{bf['tented_vias']} tented via(s)"
        rows.append(
            _row("via_tenting", "pass", n) if c == "supported" else
            _row("via_tenting", "fail", f"{n}; vendor doesn't tent vias: "
                 "open them in the board setup") if c == "not_supported" else
            _row("via_tenting", "unknown",
                 f"{n}; whether this vendor tents them is not settled"))
    if not assembly:
        return rows
    asm = prof.get("assembly")
    if not svc.get("pcba") or not asm:
        rows.append(_row("service_pcba", "fail", "no assembly service"))
        return rows
    if bf["bottom_smd_refs"]:
        sides = asm.get("sides")
        rows.append(_row("bottom_side", "unknown" if not sides else
                         "pass" if "bottom" in sides else "fail",
                         f"{len(bf['bottom_smd_refs'])} SMD parts on the "
                         f"bottom; sides: {sides or 'not published'}"))
    lim = asm.get("min_board_mm")
    if lim:
        ok = _fits_size(w, h, lim, False)
        rows.append(_row("assembly_min_size", "pass" if ok else "fail",
                         f"{w} x {h} mm; assembly needs {lim[0]} x {lim[1]}"
                         + ("" if ok else " (panelise it)")))
    if asm.get("upload") == "native" and bf.get("refs_without_mpn"):
        refs = bf["refs_without_mpn"]
        rows.append(_row("native_bom", "unknown",
                         f"{meta.get('name')} reads the BOM from the board "
                         f"file and {len(refs)} footprints carry no MPN "
                         f"field ({', '.join(refs[:8])}"
                         f"{'...' if len(refs) > 8 else ''}): pick them in "
                         "its BOM tool or add MPN fields"))
    mp, pp = asm.get("min_pitch_mm"), bf["min_pitch_mm"]
    if pp is not None:
        rows.append(_row("pitch", "unknown" if mp is None else
                         "pass" if pp + 1e-3 >= float(mp) else "fail",
                         f"finest pad pitch {pp} mm ({bf['min_pitch_ref']}); "
                         f"vendor places down to "
                         f"{mp if mp is not None else 'unpublished'} mm"))
    return rows


def dfm_rows(pcb: Path, gerbers: Path | None, vendor: str,
             why_not: str | None) -> tuple[list[dict], dict | None]:
    """dfm_check's fab-rule families against this vendor's minimums."""
    if gerbers is None:
        return [_row("dfm", "unknown", why_not or "no gerbers")], None
    import dfm_check
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rep = dfm_check.run(pcb, fab_dir=gerbers, polarity=False,
                                skip=DFM_SKIP, vendor=vendor)
    except Exception as exc:  # noqa: BLE001 - one vendor's error isn't fatal
        return [_row("dfm", "unknown",
                     f"dfm_check could not run: {type(exc).__name__}: "
                     f"{exc}")], None
    errs = [v for v in rep.get("violations", [])
            if v.get("severity") == "error"]
    kinds: dict[str, int] = {}
    for v in errs:
        kinds[v.get("kind", "?")] = kinds.get(v.get("kind", "?"), 0) + 1
    detail = (f"{rep.get('capability_key')}: " +
              (", ".join(f"{k} x{c}" for k, c in sorted(kinds.items()))
               if kinds else "no errors against this fab's minimums"))
    summary = {"status": rep.get("status"), "errors": kinds,
               "capability_key": rep.get("capability_key"),
               "examples": [v.get("msg") for v in errs[:5]]}
    return [_row("dfm", "fail" if errs else "pass", detail)], summary


def price(pcb: Path, vendor: str, qtys: list[int], assembly: bool,
          parts: Path | None) -> dict:
    import order_quote
    quote_url = vendors.load(vendor)["meta"].get("quote_url")
    boards_only = False
    try:
        try:
            rep = order_quote.run(pcb, qtys, ["HASL"], ["green"],
                                  assembly=assembly, parts_json=parts,
                                  vendor=vendor)
        except ValueError as exc:
            if not (assembly and "assembly prices" in str(exc)):
                raise
            # the boards have a published price, the assembly doesn't
            boards_only = True
            rep = order_quote.run(pcb, qtys, ["HASL"], ["green"],
                                  vendor=vendor)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "quote_url": quote_url,
                "why": f"{type(exc).__name__}: {exc}"}
    out = {"status": rep.get("status"), "estimated": rep.get("estimated"),
           "currency": rep.get("currency"),
           "quote_url": rep.get("authoritative_quote_url"),
           "verified": rep.get("pricing_verified")}
    best: dict[int, float] = {}
    for r in rep.get("matrix") or []:
        q = r["qty"]
        if q not in best or r["total"] < best[q]:
            best[q] = r["total"]
    out["by_qty"] = {str(q): t for q, t in sorted(best.items())}
    if boards_only:
        out["boards_only"] = True
    if rep.get("calibration"):
        out["calibration"] = rep["calibration"].get("observed_underestimate")
    return out


def fit(pcb: Path, fab_dir: Path | None = None,
        names: list[str] | None = None, assembly: bool = False,
        qtys: tuple[int, ...] = (5, 10), parts: Path | None = None,
        dfm: bool = True) -> dict:
    profs = vendors.load_all(names)
    bf = board_facts(pcb)
    with tempfile.TemporaryDirectory(prefix="hwde_vendor_") as td:
        gerbers, why_not = None, None
        if not dfm:
            why_not = "skipped (--no-dfm)"
        elif fab_dir is not None:
            gerbers = Path(fab_dir)
        else:
            import fab_export
            try:
                man = fab_export.run(pcb, Path(td) / "fab", make_zip=False,
                                     allow_stale=True)   # scratch, not a release
                gerbers = Path(man["gerber_dir"])
            except Exception as exc:  # noqa: BLE001
                why_not = f"gerber export failed: {type(exc).__name__}: {exc}"
        results = []
        for prof in profs:
            vid = prof["meta"]["id"]
            rows = limits(prof, bf, assembly)
            drows, dsum = dfm_rows(pcb, gerbers, vid, why_not)
            rows += drows
            statuses = {r["status"] for r in rows}
            verdict = ("fail" if "fail" in statuses else
                       "unknown" if "unknown" in statuses else "fits")
            ent = {"vendor": vid, "name": prof["meta"].get("name"),
                   "verdict": verdict, "checks": rows,
                   "profile_verified": prof["meta"].get("verified"),
                   "source_urls": prof["meta"].get("source_urls"),
                   "price": price(pcb, vid, list(qtys), assembly, parts)}
            if dsum:
                ent["dfm"] = dsum
            results.append(ent)
    fits = [r["vendor"] for r in results if r["verdict"] == "fits"]
    payload = {"script": SCRIPT, "status": "pass" if fits else "violations",
               "board": pcb.name, "assembly": assembly, "board_facts": bf,
               "fits": fits, "vendors": results}
    ws = ledgerlib.workspace_of(pcb)
    if ws is not None:
        basis = ledgerlib.board_basis(ws)
        facts = []
        for r in results:
            bad = [c for c in r["checks"] if c["status"] != "pass"]
            facts.append({
                "subject": r["vendor"], "kind": "vendor", "refs": [],
                "basis": basis,
                "status": {"fits": "verified", "fail": "failed"}.get(
                    r["verdict"], "unknown"),
                "source": f"vendor.py fit against reference/vendors/"
                          f"{r['vendor']}.yaml (copied "
                          f"{r['profile_verified']})"
                          + (" with assembly" if assembly else ""),
                "value": "; ".join(f"{c['check']}: {c['detail']}"
                                   for c in bad) or "every limit met"})
        ledgerlib.put(ws, facts, SCRIPT)
    return payload


def fit_table(rep: dict) -> str:
    lines = [f"{rep['board']}: {rep['board_facts']['layers']} layers, "
             f"{rep['board_facts']['width_mm']} x "
             f"{rep['board_facts']['height_mm']} mm, "
             f"{rep['board_facts']['copper_oz']:g} oz"
             + (", assembled" if rep["assembly"] else ""),
             "", "| vendor | verdict | not met / unknown | estimate |",
             "|---|---|---|---|"]
    for r in rep["vendors"]:
        bad = "; ".join(f"{c['check']}: {c['detail']}" for c in r["checks"]
                        if c["status"] != "pass") or "-"
        pr = r["price"]
        est = (", ".join(f"{q}: {pr.get('currency', '')} {t}"
                         for q, t in pr.get("by_qty", {}).items())
               or ("quote: " + str(pr.get("quote_url"))))
        if pr.get("boards_only"):
            est += " (boards only; assembly: quote)"
        lines.append(f"| {r['name']} | {r['verdict']} | {bad} | {est} |")
    return "\n".join(lines)


# ------------------------------------------------------------- packaging

# column meaning -> the names it goes by (docs/vendors.md)
MEANINGS = {
    "designator": ("designator", "designators", "reference", "references",
                   "refdes", "ref des", "ref", "part/designator", "parts"),
    "qty": ("qty", "quantity", "qty per board", "quantity per board"),
    "manufacturer": ("manufacturer", "mfr", "manufacturer name", "mfg",
                     "manufacturer(optional)"),
    "mpn": ("mpn", "mfg part #", "mfr part #", "manufacturer part number",
            "mfr part number", "mfg part number", "part number",
            "manufacturer part #", "*manufacturer part number",
            "mpn/seeed sku", "manufacturer part number/seeed sku"),
    "lcsc": ("lcsc", "lcsc part #", "lcsc part number"),
    "value": ("comment", "value", "val", "description",
              "description / value", "description/value", "*value"),
    "footprint": ("footprint", "package", "package/footprint",
                  "package / footprint", "*package"),
    "type": ("type", "mount type", "mounting type", "smd/tht"),
    "notes": ("notes", "your instructions / notes", "instructions", "note",
              "comments"),
    "item": ("item", "item #", "no.", "#", "line"),
    "x": ("mid x", "posx", "x", "center-x(mm)", "center x", "x (mm)"),
    "y": ("mid y", "posy", "y", "center-y(mm)", "center y", "y (mm)"),
    "side": ("layer", "side", "top/bottom"),
    "rotation": ("rotation", "rot", "angle"),
}


def meaning(col: str) -> str | None:
    c = col.strip().lower()
    for m, names in MEANINGS.items():
        if c in names:
            return m
    return None


def _fab_files(ws: Path) -> dict:
    fab = ws / "fab"
    if not fab.is_dir():
        raise checklib.CheckError(f"{ws}: no fab/ - run the fab step first")
    zips = sorted(fab.glob("*_gerbers.zip")) or sorted(fab.glob("*.zip"))
    pos = sorted(fab.glob("*-pos.csv"))
    return {"fab": fab, "zip": zips[0] if zips else None,
            "gerbers": fab / "gerbers" if (fab / "gerbers").is_dir()
            else None,
            "bom_full": fab / "BOM-full.csv"
            if (fab / "BOM-full.csv").is_file() else None,
            "pos": pos[0] if pos else None,
            "cpl_jlc": fab / "CPL.csv" if (fab / "CPL.csv").is_file()
            else None}


def _bom_rows(bom_full: Path) -> list[dict]:
    with bom_full.open(newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh)
                if (r.get("Assembly Class") or "smt_placed") in ASSEMBLED]


def _write_csv(cols: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    w.writerows(rows)
    return buf.getvalue()


def vendor_bom(asm: dict, rows: list[dict], warn: list[str]) -> str:
    cols = (asm.get("bom") or {}).get("columns") or \
        ["Designator", "Qty", "Manufacturer", "MPN", "Value", "Footprint"]
    for c in cols:
        if meaning(c) is None:
            warn.append(f"BOM column {c!r}: no known meaning, left empty")
    key = asm.get("part_number", "mpn")
    out = []
    for i, r in enumerate(rows, 1):
        refs = [x.strip() for x in (r.get("Designator") or "").split(",")
                if x.strip()]
        if key == "mpn" and not (r.get("MPN") or "").strip():
            warn.append(f"{','.join(refs)}: no MPN - the vendor can't "
                        "source it; add one or supply the part")
        cls = r.get("Assembly Class") or "smt_placed"
        val = {"designator": ",".join(refs),
               "qty": r.get("Qty Per Board") or len(refs),
               "manufacturer": r.get("Manufacturer", ""),
               "mpn": r.get("MPN", ""), "lcsc": r.get("LCSC", ""),
               "value": r.get("Comment", ""),
               "footprint": r.get("Footprint", ""),
               "type": "THT" if cls == "tht" else "SMD",
               "notes": r.get("Instructions", ""), "item": i}
        out.append([val.get(meaning(c) or "", "") for c in cols])
    return _write_csv(cols, out)


def _manufacturers(ws: Path, rows: list[dict]) -> None:
    """Fill each row's Manufacturer from the workspace's LCSC data (the
    same lookup distributor_bom.py makes for the DigiKey/Mouser files)."""
    import distributor_bom
    idx = distributor_bom.lcsc_index(ws / "parts")
    for r in rows:
        ent = idx.get((r.get("LCSC") or "").strip()) or {}
        r.setdefault("Manufacturer", ent.get("manufacturer", ""))
        if not (r.get("MPN") or "").strip() and ent.get("mpn"):
            r["MPN"] = ent["mpn"]


def vendor_cpl(asm: dict, pos: Path, refs: set[str],
               jlc_cpl: Path | None, warn: list[str]) -> str:
    cols = (asm.get("cpl") or {}).get("columns") or \
        ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
    labels = {"top": "Top", "bottom": "Bottom",
              **((asm.get("cpl") or {}).get("side_labels") or {})}
    for c in cols:
        if meaning(c) is None:
            warn.append(f"CPL column {c!r}: no known meaning, left empty")
    jlc_rot = {}
    if asm.get("rotation") == "jlc":
        if jlc_cpl is None:
            raise checklib.CheckError("this vendor takes JLC rotations and "
                                      "fab/CPL.csv is missing")
        with jlc_cpl.open(newline="", encoding="utf-8") as fh:
            jlc_rot = {r["Designator"]: r["Rotation"]
                       for r in csv.DictReader(fh)}
    out, seen = [], set()
    with pos.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ref = r.get("Ref", "")
            if ref not in refs:
                continue
            seen.add(ref)
            rot = float(r.get("Rot") or 0.0) % 360.0
            val = {"designator": ref, "x": f"{float(r['PosX']):.4f}",
                   "y": f"{float(r['PosY']):.4f}",
                   "side": labels.get((r.get("Side") or "top").lower(),
                                      r.get("Side")),
                   "rotation": jlc_rot.get(ref, f"{rot:.4f}"),
                   "value": r.get("Val", ""),
                   "footprint": r.get("Package", "")}
            out.append([val.get(meaning(c) or "", "") for c in cols])
    missing = sorted(refs - seen)
    if missing:
        warn.append(f"not in the pos file, so not placed: {missing}")
    return _write_csv(cols, out)


def package(ws: Path, vendor: str, out_dir: Path | None = None) -> dict:
    ws = Path(ws)
    prof = vendors.load(vendor)
    meta = prof["meta"]
    files = _fab_files(ws)
    out = Path(out_dir) if out_dir else files["fab"] / vendor
    out.mkdir(parents=True, exist_ok=True)
    warn: list[str] = []
    written = []
    gz = out / "gerbers.zip"
    if files["zip"] is not None:
        shutil.copyfile(files["zip"], gz)
    elif files["gerbers"] is not None:
        with zipfile.ZipFile(gz, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(files["gerbers"].iterdir()):
                if f.is_file():
                    z.write(f, f.name)
    else:
        raise checklib.CheckError(f"{files['fab']}: no gerbers to package")
    written.append(gz.name)
    asm = prof.get("assembly")
    assembled = bool(asm and (meta.get("services") or {}).get("pcba"))
    rows: list[dict] = []
    native = assembled and asm.get("upload") == "native"
    if native:
        # the vendor builds its BOM and placement from the board file
        pcb = ledgerlib.board_pcb(ws)
        if pcb is None:
            raise checklib.CheckError(f"{ws}/kicad: not exactly one board")
        for f in (pcb, pcb.with_suffix(".kicad_pro")):
            if f.is_file():
                shutil.copyfile(f, out / f.name)
                written.append(f.name)
        missing = _refs_without_mpn(pcb)
        if missing:
            warn.append(f"{len(missing)} footprints carry no MPN field "
                        f"({', '.join(missing[:12])}): {meta.get('name')} "
                        "reads parts from the board file")
    elif assembled and files["bom_full"] is not None:
        rows = _bom_rows(files["bom_full"])
        _manufacturers(ws, rows)
        safelib.atomic_write_text(out / "BOM.csv", vendor_bom(asm, rows, warn))
        written.append("BOM.csv")
        refs = {x.strip() for r in rows
                for x in (r.get("Designator") or "").split(",") if x.strip()}
        if files["pos"] is not None:
            safelib.atomic_write_text(out / "CPL.csv", vendor_cpl(
                asm, files["pos"], refs, files["cpl_jlc"], warn))
            written.append("CPL.csv")
        else:
            warn.append("no <board>-pos.csv in fab/: no CPL written")
    elif assembled:
        warn.append("no fab/BOM-full.csv: bare-board package only")
    order = order_md(ws, prof, rows, warn, written)
    safelib.atomic_write_text(out / "ORDER.md", order)
    written.append("ORDER.md")
    return {"script": SCRIPT, "status": "pass", "vendor": vendor,
            "out_dir": str(out), "files": written, "warnings": warn}


def order_md(ws: Path, prof: dict, rows: list[dict], warn: list[str],
             written: list[str]) -> str:
    meta = prof["meta"]
    asm = prof.get("assembly") or {}
    files = prof.get("files") or {}
    pcb = ledgerlib.board_pcb(ws)
    fact = None
    if pcb is not None:
        f = ledgerlib.load(ws)["facts"].get(ledgerlib.key(meta["id"],
                                                         "vendor"))
        if f:
            fact = ledgerlib.effective(f, {"basis": ledgerlib.board_basis(ws)})
    L = [f"# Ordering {ws.name} from {meta.get('name')}", "",
         f"Quote and order: {meta.get('quote_url')}",
         f"Vendor rules copied {meta.get('verified')} from "
         + ", ".join(meta.get("source_urls") or []) + ".", "",
         "## Files", ""]
    L += [f"- `{f}`" for f in written if f != "ORDER.md"]
    if files.get("accepts_native") and asm.get("upload") != "native":
        L.append(f"- {meta.get('name')} also takes "
                 f"{', '.join(files['accepts_native'])} as-is; the gerbers "
                 "are what hwde checked, so upload those.")
    L += ["", "## Before ordering", ""]
    if fact is None:
        L.append(f"- [ ] `hwde vendor fit` has not judged this board for "
                 f"{meta.get('name')}: run it.")
    else:
        L.append(f"- [{'x' if fact['status'] == 'verified' else ' '}] "
                 f"vendor fit: {fact['status']}"
                 + (f" ({fact.get('why') or fact.get('value')})"
                    if fact["status"] != "verified" else ""))
    if asm.get("upload") == "native" and \
            (meta.get("services") or {}).get("pcba"):
        L.append(f"- [ ] For assembly, upload the `.kicad_pcb`: "
                 f"{meta.get('name')} takes no BOM/CPL beside gerbers and "
                 "builds both from the board file (its fields and pad 1). "
                 "Check its BOM tool's part picks and polarity preview.")
    if rows:
        rot = asm.get("rotation", "kicad")
        L.append("- [ ] Ask for the vendor's placement review: the CPL "
                 + ("carries JLC's corrected rotations."
                    if rot == "jlc" else
                    "carries KiCad's own rotations (IPC-7351 zero, as "
                    "exported), which the vendor's engineers check against "
                    "the silkscreen; hwde has not checked this vendor's "
                    "zero for each package."))
        if asm.get("zero_orientation"):
            L.append(f"- [ ] {meta.get('name')} places from "
                     f"{asm['zero_orientation']}: expect multi-pin parts "
                     "to need turning in its placement editor.")
        src = asm.get("sourcing", "?")
        L.append(f"- [ ] Parts: {src}; keyed on "
                 f"{asm.get('part_number', 'mpn').upper()}; distributors "
                 f"{', '.join(asm.get('distributors') or ['not published'])}."
                 " Check stock at the quote: hwde's live stock check covers "
                 "JLC/LCSC only.")
        consigned = [r.get("Designator") for r in rows
                     if "supplied" in (r.get("Instructions") or "").lower()]
        if consigned:
            L.append(f"- [ ] Ship in the consigned parts: {consigned}")
    if warn:
        L += ["", "## Warnings", ""] + [f"- {w}" for w in warn]
    lt = prof.get("lead_time_days") or {}
    if lt:
        L += ["", f"Lead time (build only, from the profile): {lt}"]
    return "\n".join(L) + "\n"


def choose(ws: Path, vendor: str) -> dict:
    ws = Path(ws)
    vendors.path(vendor)                         # exists, or VendorError
    st = ws / "state.json"
    if not st.is_file():
        raise checklib.CheckError(f"{ws}: no state.json - not a workspace")
    with safelib.writer_lock(st):
        data = json.loads(st.read_text(encoding="utf-8"))
        prev = data.get("vendor")
        data["vendor"] = vendor
        data.setdefault("decisions", []).append(
            {"what": f"vendor: {vendor}", "why": "vendor.py choose",
             "phase": data.get("phase"), "ts": safelib.now_iso()})
        safelib.atomic_write_json(st, data)
    rep = ledgerlib.report(ws)
    f = next((r for r in rep["facts"] if r["kind"] == "vendor"), None)
    return {"script": SCRIPT, "status": "pass", "vendor": vendor,
            "previous": prev, "fact": f}


# ------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("show")
    p.add_argument("vendor")
    p = sub.add_parser("fit")
    p.add_argument("--pcb", required=True)
    p.add_argument("--fab-dir")
    p.add_argument("--vendors", default="all")
    p.add_argument("--assembly", action="store_true")
    p.add_argument("--parts", help="parts.json (assembly feeder fees)")
    p.add_argument("--qty", default="5,10")
    p.add_argument("--no-dfm", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="print the JSON report, not the table")
    p.add_argument("--out")
    p = sub.add_parser("package")
    p.add_argument("--workspace", required=True)
    p.add_argument("--vendor", required=True)
    p.add_argument("--out-dir")
    p = sub.add_parser("choose")
    p.add_argument("--workspace", required=True)
    p.add_argument("--vendor", required=True)
    a = ap.parse_args(argv)
    checklib.utf8_stdout()
    try:
        if a.cmd == "list":
            for prof in vendors.load_all():
                m, b = prof["meta"], prof["capabilities"]["board"]
                svc = ",".join(k for k, v in (m.get("services") or {})
                               .items() if v)
                print(f"{m['id']:14} {m.get('name', ''):14} {svc:18} "
                      f"layers {b.get('layers', '?')}  "
                      f"price {prof['pricing'].get('model')}  "
                      f"copied {m.get('verified')}")
            return 0
        if a.cmd == "show":
            prof = vendors.load(a.vendor)
            print(json.dumps(prof, indent=1, default=str))
            return 0
        if a.cmd == "fit":
            names = None if a.vendors == "all" else \
                [v.strip() for v in a.vendors.split(",") if v.strip()]
            rep = fit(Path(a.pcb), Path(a.fab_dir) if a.fab_dir else None,
                      names, a.assembly,
                      tuple(int(q) for q in a.qty.split(",") if q.strip()),
                      Path(a.parts) if a.parts else None, not a.no_dfm)
            if a.out:
                safelib.atomic_write_json(Path(a.out), rep)
            print(json.dumps(rep, indent=1) if a.json else fit_table(rep))
            return 0 if rep["fits"] else 1
        if a.cmd == "package":
            rep = package(Path(a.workspace), a.vendor,
                          Path(a.out_dir) if a.out_dir else None)
        else:
            rep = choose(Path(a.workspace), a.vendor)
        print(json.dumps(rep, indent=1))
        for w in rep.get("warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        return 0
    except (vendors.VendorError, checklib.CheckError, OSError,
            ValueError) as exc:
        print(json.dumps({"script": SCRIPT, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
