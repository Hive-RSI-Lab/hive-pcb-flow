#!/usr/bin/env python
"""stock_check.py - can JLC place every row of the BOM today? (the fab gate)

Reads the assembler upload (fab/BOM.csv: Comment, Designator, Footprint,
LCSC Part #) and asks JLC's and LCSC's own records, live, for each row
(lib/stocklib.py): is the code JLC's and for sale, is it the part the BOM
means (MPN, else value), and can JLC sell what the build needs (designators
x --boards)? A row JLC would swap (not for sale, with its substitute), short,
unknown to JLC, mismatched, or not checkable (a source down) fails: unknown
is never a pass. bom_cpl.py runs this on every fab run it makes live; run it
again right before the order, since stock moves daily.

The MPN a row is compared with comes from the workspace's parts data (parts/
parts.json, parts/<LCSC>.json - distributor_bom.lcsc_index); a passive with
no MPN is matched by its value in JLC's description.

With --workspace the report goes to <ws>/reports/stock_check.json unless
--out says otherwise, and the verdicts go into the board's fact ledger
(ledger.json, lib/ledgerlib.py) as `stock`, `part_match` and `lifecycle`
facts, each with its source and time.

--mpn checks a BOM keyed by MPN instead (a US-sourced one: an MPN or
Manufacturer Part Number column, Designator(s), Buy), against the
distributor pages that answer without an API key (lib/keyless_stock.py):
a row is ok only when one authorized distributor holds the whole need
(--country US: in that country's stock; --brokers: brokers count too),
short when none does, not_found when the page lists no offer with that
exact MPN, and not_judged, blocking, when no page answers. Rows marked
Buy=no or Assembly Class dnp/board_feature are skipped. No ledger facts
are written in this mode (the ledger keys stock by LCSC code).

CLI:
  stock_check.py --bom fab/BOM.csv [--parts parts/] [--boards 5]
                 [--workspace WS] [--jobs 6] [--out report.json]
  stock_check.py --mpn --bom fab/out/BOM-US.csv [--boards 5]
                 [--country US] [--brokers] [--out report.json]
Exit 0 every row ok / 1 a row blocks the order / 2 error (no BOM, no
LCSC column, or with --mpn no MPN column). HWDE_STOCK_OFFLINE=1 refuses
(exit 2): this check is live or it is nothing.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import stocklib  # noqa: E402

SCRIPT = "stock_check"
DEFAULT_BOARDS = 5   # bom_cpl's DEFAULT_BUILD_QTY: JLC's smallest PCBA order
LCSC_COLS = ("LCSC Part #", "LCSC", "JLCPCB Part #", "LCSC Part")


def read_bom(path: Path, ws: Path | None = None) -> list[dict]:
    """The BOM's rows; with ws, a code-less row's holes, test points,
    fiducials and DNP refs are dropped (they need no LCSC code)."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        col = next((c for c in LCSC_COLS if c in r), None)
        if col is None:
            raise ValueError(f"{path}: no LCSC column (one of "
                             f"{', '.join(LCSC_COLS)})")
        refs = [x.strip() for x in str(r.get("Designator") or "").split(",")
                if x.strip()]
        if ws is not None and not (r.get(col) or "").strip():
            import ledgerlib
            kept = ledgerlib.codeless_needed(ws, refs)
            if refs and not kept:
                continue
            refs = kept
        out.append({"Comment": r.get("Comment", ""),
                    "Designator": r.get("Designator", ""),
                    "Footprint": r.get("Footprint", ""),
                    "LCSC": (r.get(col) or "").strip(), "refs": refs})
    return out


MPN_COLS = ("MPN", "Manufacturer Part Number", "Mfr Part Number",
            "Manufacturer_Part_Number", "Mfr. Part #")
SKIP_CLASSES = ("dnp", "board_feature")


def read_mpn_bom(path: Path) -> list[dict]:
    """An MPN-keyed BOM's bought rows: {mpn, refs, qty, value}."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    col = next((c for c in MPN_COLS if rows and c in rows[0]), None)
    if col is None:
        raise ValueError(f"{path}: no MPN column (one of "
                         f"{', '.join(MPN_COLS)})")
    out = []
    for r in rows:
        if str(r.get("Buy") or "yes").strip().lower().startswith("no"):
            continue
        if (r.get("Assembly Class") or "").strip().lower() in SKIP_CLASSES:
            continue
        refs = [x for x in re.split(r"[,\s]+", str(
            r.get("Designators") or r.get("Designator") or "")) if x]
        mpn = (r.get(col) or "").strip()
        try:
            qty = int(str(r.get("Qty") or r.get("Quantity") or "").strip())
        except ValueError:
            qty = None
        out.append({"mpn": mpn, "refs": refs, "qty": qty or len(refs),
                    "value": r.get("Value") or r.get("Comment") or ""})
    return out


def check_mpn_rows(rows: list[dict], boards: int = DEFAULT_BOARDS,
                   fetch=None, jobs: int = 6, country: str | None = None,
                   brokers: bool = False) -> dict:
    """check_rows' report for MPN rows, judged by lib/keyless_stock."""
    import keyless_stock
    fetch = fetch or keyless_stock.http_text

    def one(r):
        need = (len(r["refs"]) or r["qty"] or 1) * boards
        if not r["mpn"]:
            v = {"mpn": "", "need": need, "status": "not_judged",
                 "why": "the row names no MPN"}
        else:
            v = keyless_stock.judge(r["mpn"], need, fetch=fetch,
                                    country=country, brokers=brokers)
        return {**v, "refs": r["refs"], "comment": r["value"]}

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        verdicts = list(ex.map(one, rows))
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    blocking = [v for v in verdicts
                if v["status"] in keyless_stock.BLOCKING]
    return {"status": "fail" if blocking else "pass", "boards": boards,
            "mode": "mpn", "country": country, "brokers": brokers,
            "checked_at": stocklib.now(), "counts": counts,
            "rows": verdicts,
            "blocking": [{"lcsc": "", "mpn": v["mpn"], "refs": v["refs"],
                          "status": v["status"], "why": v["why"]}
                         for v in blocking]}


def mpn_index(parts_dir: Path | None) -> dict[str, dict]:
    import distributor_bom
    return distributor_bom.lcsc_index(parts_dir)


def check_rows(rows: list[dict], boards: int = DEFAULT_BOARDS,
               mpns: dict | None = None, fetch=None,
               jobs: int = 6) -> dict:
    """{status, boards, rows[], blocking[], counts{}} for BOM rows (dicts
    with Comment, LCSC and refs or Designator)."""
    fetch = fetch or stocklib.http_fetch
    mpns = mpns or {}

    def one(r):
        refs = r.get("refs") or [x.strip() for x in
                                 str(r.get("Designator") or "").split(",")
                                 if x.strip()]
        code = str(r.get("LCSC") or "").strip()
        mpn = (mpns.get(code) or {}).get("mpn", "")
        v = stocklib.check_row(code, len(refs) * boards, mpn=mpn,
                               value=r.get("Comment", ""), fetch=fetch,
                               footprint=r.get("Footprint", ""))
        v["refs"] = refs
        v["comment"] = r.get("Comment", "")
        return v

    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        verdicts = list(ex.map(one, rows))
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    blocking = [v for v in verdicts if v["status"] in stocklib.BLOCKING]
    return {"status": "fail" if blocking else "pass", "boards": boards,
            "checked_at": stocklib.now(), "counts": counts,
            "rows": verdicts,
            "blocking": [{"lcsc": v["lcsc"], "refs": v["refs"],
                          "status": v["status"], "why": v["why"]}
                         for v in blocking]}


def violations(result: dict) -> list[dict]:
    """bom_cpl-style violations, one per blocking row."""
    return [{"kind": f"bom_stock_{b['status']}", "refs": b["refs"],
             "lcsc": b["lcsc"], "message": f"{b['lcsc']}: {b['why']}"}
            for b in result.get("blocking") or []]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bom", help="JLC BOM upload (default <ws>/fab/BOM.csv)")
    ap.add_argument("--parts", help="parts dir (default <ws>/parts)")
    ap.add_argument("--workspace", help="board workspace: defaults the paths "
                    "and the report (reports/stock_check.json)")
    ap.add_argument("--boards", type=int, default=DEFAULT_BOARDS,
                    help=f"boards in the build (default {DEFAULT_BOARDS})")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", help="write the JSON report here too")
    ap.add_argument("--mpn", action="store_true",
                    help="the BOM is keyed by MPN: ask the keyless "
                    "distributor pages (lib/keyless_stock.py)")
    ap.add_argument("--country", help="with --mpn: count only stock held "
                    "in this country (two letters, e.g. US)")
    ap.add_argument("--brokers", action="store_true",
                    help="with --mpn: count brokers' stock too")
    args = ap.parse_args(argv)
    ws = Path(args.workspace) if args.workspace else None
    try:
        if stocklib.offline():
            raise RuntimeError("HWDE_STOCK_OFFLINE is set: stock is only "
                               "known by asking")
        bom = Path(args.bom) if args.bom else (ws / "fab" / "BOM.csv"
                                               if ws else None)
        if bom is None or not bom.is_file():
            raise FileNotFoundError(f"no BOM: {bom} (give --bom or "
                                    "--workspace)")
        if args.mpn:
            res = check_mpn_rows(read_mpn_bom(bom), boards=args.boards,
                                 jobs=args.jobs, country=args.country,
                                 brokers=args.brokers)
            res = {"script": SCRIPT, "bom": str(bom), **res}
            if ws is not None and not args.out:
                args.out = str(ws / "reports" / "stock_check_mpn.json")
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            return _finish(res, args.out)
        parts = Path(args.parts) if args.parts else (
            ws / "parts" if ws else bom.resolve().parent.parent / "parts")
        res = check_rows(read_bom(bom, ws), boards=args.boards,
                         mpns=mpn_index(parts), jobs=args.jobs)
        res = {"script": SCRIPT, "bom": str(bom), **res}
        if ws is not None:
            import ledgerlib
            led = ledgerlib.record_stock(ws, res)
            res["ledger"] = str(led) if led else None
        if ws is not None and not args.out:
            args.out = str(ws / "reports" / "stock_check.json")
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        err = {"script": SCRIPT, "status": "error",
               "error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(err, indent=1)
        (Path(args.out).write_text(text, encoding="utf-8") if args.out
         else print(text))
        return 2
    return _finish(res, args.out)


def _finish(res: dict, out: str | None) -> int:
    text = json.dumps(res, indent=1, ensure_ascii=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)
    for b in res["blocking"]:
        print(f"stock_check: BLOCKS {','.join(b['refs'])} "
              f"{b['lcsc'] or b.get('mpn', '')} "
              f"[{b['status']}]: {b['why']}", file=sys.stderr)
    return 1 if res["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
