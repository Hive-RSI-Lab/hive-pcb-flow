#!/usr/bin/env python
"""ledger.py - the board's fact ledger: what it relies on, and what is unknown.

lib/ledgerlib.py says what the ledger holds and who writes it. This shows it
and takes facts recorded by hand.

  ledger.py report --workspace WS [--all] [--out FILE]
      every fact the board relies on as it reads now (stale stock and facts
      about a since-changed part read unknown), with counts per kind and the
      failed, unknown and listed ones shown. Exit 0 ready (none failed or
      unknown) / 1 not ready / 2 error. Release (attest.py) refuses on the
      same failed and unknown list.
  ledger.py set --workspace WS --subject U8 --kind pinout
                --status verified|listed|failed|unknown --source "..." [--value ".."]
                [--force "why"]
      record one fact you checked yourself; --source is required for
      all but unknown (where it came from: a datasheet page, a
      calculation, a distributor page and its date). verified is refused
      (exit 2) when its datasheet PDF is suspect or a host placeholder: the
      part's own for rating and pinout, the extraction's source_pdf, any
      parts/datasheets/*.pdf --source names. --force with a reason records it
      anyway, reason on the fact.
  ledger.py prune --workspace WS [--apply]
      facts about a part no longer on the board (a swapped or removed refdes
      or LCSC code) are "orphaned": report lists them, and they never count
      as evidence. prune lists them and, with --apply, deletes them from
      ledger.json (dry run by default).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import check_ratings  # noqa: E402
import datasheet_extract  # noqa: E402
import ledgerlib  # noqa: E402

SCRIPT = "ledger"


def doubted_datasheets(ws: Path, kind: str, lcsc: str | None,
                       source: str) -> dict[str, str]:
    """{datasheet: why} for the suspect or placeholder PDFs a fact would rest
    on: for a pinout the part's own and its extraction's source_pdf, for a
    rating the PDF its extraction was read from (a rating with no extraction
    read nothing from a PDF, #799), and any PDF in the workspace that --source
    names."""
    names = set(re.findall(r"datasheets/([^/\s]+)\.pdf", source))
    if kind in ("rating", "pinout") and lcsc:
        if kind == "pinout":
            names.add(lcsc)
        try:
            ext = json.loads((ws / "parts" / f"{lcsc}.json").read_text(
                encoding="utf-8"))
            names.add(check_ratings.extraction_pdf(ext, lcsc)
                      if kind == "rating" else
                      Path(str(ext.get("source_pdf") or lcsc)).stem)
        except (OSError, ValueError, AttributeError):
            pass
    return datasheet_extract.suspect_datasheets(ws / "parts", names)


def run(argv=None) -> tuple[dict, str | None, int]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report", help="what the board knows and does not")
    p.add_argument("--workspace", required=True)
    p.add_argument("--all", action="store_true",
                   help="list every fact, not only the gaps")
    p.add_argument("--out")
    p = sub.add_parser("prune", help="drop facts of parts no longer on board")
    p.add_argument("--workspace", required=True)
    p.add_argument("--apply", action="store_true",
                   help="write ledger.json (default: dry run)")
    p.add_argument("--out")
    p = sub.add_parser("set", help="record a fact by hand")
    p.add_argument("--workspace", required=True)
    p.add_argument("--subject", required=True,
                   help="refdes (rating/pinout/footprint) or LCSC code")
    p.add_argument("--kind", required=True, choices=ledgerlib.KINDS)
    p.add_argument("--status", required=True, choices=ledgerlib.STATUSES)
    p.add_argument("--source", default="")
    p.add_argument("--value", default="")
    p.add_argument("--force", metavar="REASON",
                   help="record verified over a suspect datasheet, and why")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    ws = Path(a.workspace)
    if not (ws / "state.json").is_file():
        raise SystemExit(f"{ws}: no state.json - not a board workspace")
    if a.cmd == "prune":
        found = ledgerlib.orphans(ws)
        rows = [{"key": k, "status": f.get("status")}
                for k, f in sorted(found.items())]
        removed = ledgerlib.prune(ws, a.apply)
        rep = {"script": SCRIPT, "status": "pass", "applied": a.apply,
               "orphaned": rows, "removed": removed if a.apply else []}
        return rep, a.out, 0
    if a.cmd == "set":
        if a.status != "unknown" and not a.source.strip():
            raise SystemExit("--source is required for a verified, listed or "
                             "failed fact: where does it come from?")
        exp = ledgerlib.expected(ws).get(ledgerlib.key(a.subject, a.kind))
        fact = {"subject": a.subject, "kind": a.kind, "status": a.status,
                "source": a.source, "value": a.value}
        if exp:
            fact.update(lcsc=exp.get("lcsc"), refs=exp.get("refs"))
        if a.status == "verified":
            doubt = doubted_datasheets(ws, a.kind, (exp or {}).get("lcsc"),
                                       a.source)
            if doubt and not (a.force or "").strip():
                raise SystemExit(
                    "refused: the datasheet this rests on is suspect or a "
                    "scan with nothing to judge it by ("
                    + "; ".join(f"{c}: {w}" for c, w in sorted(doubt.items()))
                    + "). Fetch another, or read it by eye and pass "
                    '--force "why" to record it')
            if doubt:
                fact.update(forced=a.force.strip(),
                            forced_over="; ".join(
                                f"{c}: {w}" for c, w in sorted(doubt.items())))
        ledgerlib.put(ws, [fact], "hand")
        rep = {"script": SCRIPT, "status": "pass", "recorded": fact,
               "expected": exp is not None}
        return rep, a.out, 0
    rep = ledgerlib.report(ws)
    if not a.all:
        rep.pop("facts")
    rep = {"script": SCRIPT, "status": "pass" if rep["ready"] else "gaps",
           **rep}
    return rep, a.out, 0 if rep["ready"] else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        rep, out, code = run(argv)
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            raise
        rep, out, code = ({"script": SCRIPT, "status": "error",
                           "error": str(exc.code)}, None, 2)
    except Exception as exc:  # noqa: BLE001 (SPEC: any error -> exit 2)
        rep, out, code = ({"script": SCRIPT, "status": "error",
                           "error": f"{type(exc).__name__}: {exc}"}, None, 2)
    text = json.dumps(rep, indent=1, ensure_ascii=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
