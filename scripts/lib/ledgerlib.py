"""ledgerlib.py - what a board knows about the parts it relies on, and how.

The person, 10-28: "we need to catch stuff like this. know what we dont
know". A board workspace keeps `ledger.json`: one entry per fact the design
relies on, each `verified` (with its source and when), `listed` (a rating
compared and held, on a number no datasheet read backs; see below), `failed`
(a check found it false) or `unknown` (nothing has checked it - including a
check that ran and could not tell). Checks write to it as they run; nothing is
assumed.

Facts and who writes them:
  stock       per LCSC code on BOM.csv: JLC can sell what the build needs
              (stock_check.py, and bom_cpl.py's live run)
  part_match  per LCSC code: the code JLC will place is the BOM's part
              (stock_check.py)
  lifecycle   per LCSC code: JLC sells the code, nothing substitutes it
              (stock_check.py)
  rating      per refdes: every connected pin within its rating, verified
              when the rating was read from a datasheet, listed when it was
              not; rated_from says which: datasheet, distributor (parts.json
              attributes), part_code (a capacitor's maker voltage code in
              its MPN, or a voltage in its Value field) or package (a
              generic table)
              (check_ratings.py, run by the verify gate)
  pinout      per refdes with more than two pins: the board's pad numbers
              and names agree with the datasheet extraction's pinout
              (check_ratings.py)
  footprint   per placed refdes: the board footprint's pads land on the
              LCSC part's own model, by number (cpl_verify.py, run by dfm)
  vendor      per chosen vendor (state.json "vendor", set by vendor.py
              choose): the board as it is now fits that fab's rules and
              limits (vendor.py fit, docs/vendors.md). Expected only once a
              vendor is chosen; a fit of an older board file reads unknown.
  Any fact can also be recorded by hand, with its source:
    ledger.py set --workspace WS --subject U8 --kind pinout --status verified
                  --source "CH340 datasheet v3B p.4, pin table vs symbol"
  A datasheet that is suspect or a placeholder can't back a verified fact:
  set refuses it unless --force says why (the reason goes on the fact).

What the board relies on (`expected`) comes from its own data: every code on
fab/BOM.csv (else every smt_placed parts.json line), and every refdes of every
parts.json line that is bought (not dnp, board_feature or customer_supplied).
A hole, test point or fiducial gets no rating or pinout fact (no datasheet
answers one), and a footprint fact only when it has an LCSC code (a model to
land on). Parts that need no LCSC code (those, or a DNP or
otherwise unbought line) get no `(no LCSC: REF)` facts; a real part with no
code gets one (`stock`, unknown: "no LCSC code"), which blocks. Recording
stock (`record_stock`, so stock_check and bom_cpl's live run) also prunes the
orphaned facts (`prune`): a gone part, a placeholder that gained a code, a
vendor no longer chosen.
A fact expected but never written reads `unknown`. A recorded fact reads
`unknown` again when it went stale: stock older than STOCK_MAX_AGE_H, or a
fact about a refdes whose LCSC code has since changed.

Every kind is critical: release (attest.py build, and order-ready, which
re-verifies) refuses while any expected fact is `failed` or `unknown`
(`blockers()`); `ledger.py report` lists them.

`listed` does not refuse (#781). A listed number was compared and held, which
an unknown wasn't, and refusing it would stop nearly every rating on the fleet
with no case of a listing disagreeing with its datasheet to justify that. But
it is not called verified: `report()` lists them, attest.py names them in its
result, and check_ratings warns once per board with the way to verified
(`datasheet_extract --fetch`, then extraction). A ledger from before the tier
reads listed too (`effective`), until check_ratings next writes it.

Writers only write into a real workspace (a state.json beside the board
folder) and never when HWDE_LEDGER_WRITE=0 (the test suite's default).
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import re
from pathlib import Path

LEDGER = "ledger.json"
SCHEMA = 1
KINDS = ("stock", "part_match", "lifecycle", "rating", "pinout", "footprint",
         "vendor")
PART_KINDS = ("stock", "part_match", "lifecycle")   # subject: LCSC code
REF_KINDS = ("rating", "pinout", "footprint")       # subject: refdes
VENDOR_KINDS = ("vendor",)                          # subject: vendor id
STATUSES = ("verified", "listed", "failed", "unknown")
STOCK_MAX_AGE_H = 72.0
ATTRIBUTE_SOURCE = "parts.json attributes of"
UNBOUGHT = ("dnp", "board_feature", "customer_supplied")
# the classes the assembler fits from the upload (bom_cpl.ASSEMBLED_CLASSES)
ASSEMBLED = ("smt_placed", "tht")
# refdes prefixes of parts with a pin map worth checking (two-terminal
# passives, LEDs and diodes have no pinout beyond polarity, which
# cpl_verify and dfm's polarity check cover)
TWO_PIN = re.compile(r"^(R|C|L|FB|D|LED|F|TP|H|MH|FID)\d", re.I)
# refdes prefixes of parts that are never on the BOM or the assembly line:
# mounting holes, test points, fiducials. With no LCSC code they need none.
NO_CODE_REF = re.compile(r"^(TP|MH|H|FID|FD)\d", re.I)
STOCK_SOURCE = ("JLC parts library record (jlcpcb.com smtGood search) and "
                "LCSC product detail (wmsc.lcsc.com)")


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str | None):
    try:
        t = _dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def key(subject: str, kind: str) -> str:
    return f"{subject}/{kind}"


def path(ws: Path) -> Path:
    return Path(ws) / LEDGER


def load(ws: Path) -> dict:
    p = path(ws)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("facts"), dict):
                return data
        except (OSError, ValueError):
            pass
    return {"schema": SCHEMA, "facts": {}}


def writable(ws: Path | None) -> bool:
    return (ws is not None and (Path(ws) / "state.json").is_file()
            and os.environ.get("HWDE_LEDGER_WRITE", "1") != "0")


def workspace_of(pcb: Path) -> Path | None:
    """<ws> for <ws>/kicad/<board>.kicad_pcb when it is a real workspace."""
    ws = Path(pcb).resolve().parent.parent
    return ws if (ws / "state.json").is_file() else None


# ------------------------------------------------------- what is relied on

def _parts(ws: Path) -> list[dict]:
    pj = Path(ws) / "parts" / "parts.json"
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("parts", data) if isinstance(data, dict) else data
    return [p for p in items or [] if isinstance(p, dict)]


def _refs(line: dict) -> list[str]:
    r = line.get("refdes") or []
    return [r] if isinstance(r, str) else [str(x) for x in r]


def _class(line: dict, ref: str) -> str:
    rc = line.get("refdes_class") or {}
    if ref in rc:
        return rc[ref]
    if ref in (line.get("refdes_dnp") or []):
        return "dnp"
    return line.get("assembly_class") or "smt_placed"


def needs_no_code(ref: str, cls: str) -> bool:
    """A part with no LCSC code that needs none: DNP or otherwise unbought,
    or a hole, test point or fiducial (no BOM or assembly line)."""
    return cls in UNBOUGHT or bool(NO_CODE_REF.match(ref))


def _class_of(ws: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in _parts(ws):
        for ref in _refs(ln):
            out[ref] = _class(ln, ref)
    return out


def codeless_needed(ws: Path, refs: list[str]) -> list[str]:
    """The refs of a code-less line that do need a code (a real part)."""
    cls = _class_of(ws)
    return [r for r in refs if not needs_no_code(r, cls.get(r, "smt_placed"))]


def _bom_codes(ws: Path) -> dict[str, list[str]] | None:
    bom = Path(ws) / "fab" / "BOM.csv"
    if not bom.is_file():
        return None
    out: dict[str, list[str]] = {}
    cls = _class_of(ws)
    with bom.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("LCSC Part #") or r.get("LCSC") or "").strip()
            refs = [x.strip() for x in str(r.get("Designator") or "")
                    .split(",") if x.strip()]
            if not code:
                refs = [x for x in refs
                        if not needs_no_code(x, cls.get(x, "smt_placed"))]
            if refs:
                out.setdefault(code or f"(no LCSC: {refs[0]})", []).extend(refs)
    return out


def expected(ws: Path) -> dict[str, dict]:
    """key -> {subject, kind, refs, lcsc}: every fact the board relies on."""
    ws = Path(ws)
    out: dict[str, dict] = {}
    lines = _parts(ws)
    lcsc_of: dict[str, str] = {}
    codes = _bom_codes(ws)
    if codes is None:
        codes = {}
        for ln in lines:
            code = str(ln.get("lcsc") or "").strip()
            placed = [r for r in _refs(ln) if _class(ln, r) in ASSEMBLED
                      and (code or not needs_no_code(r, "smt_placed"))]
            if placed:
                codes.setdefault(code or f"(no LCSC: {placed[0]})",
                                 []).extend(placed)
    for code, refs in codes.items():
        # a real part with no code is one item: JLC has nothing to look up
        for kind in (PART_KINDS if not code.startswith("(no LCSC: ")
                     else ("stock",)):
            out[key(code, kind)] = {"subject": code, "kind": kind,
                                    "refs": refs, "lcsc": code}
    for ln in lines:
        code = str(ln.get("lcsc") or "").strip()
        for ref in _refs(ln):
            cls = _class(ln, ref)
            if cls in UNBOUGHT:
                continue
            lcsc_of[ref] = code
            if NO_CODE_REF.match(ref):
                # a hole, test point or fiducial has no rating or pinout;
                # only a bought one (it has a code) has a model to land on
                kinds = (["footprint"] if code and cls in ASSEMBLED else [])
            else:
                kinds = ["rating"]
                if not TWO_PIN.match(ref):
                    kinds.append("pinout")
                if cls in ASSEMBLED:
                    kinds.append("footprint")
            for kind in kinds:
                out[key(ref, kind)] = {"subject": ref, "kind": kind,
                                       "refs": [ref], "lcsc": code}
    vendor = chosen_vendor(ws)
    if vendor:
        out[key(vendor, "vendor")] = {"subject": vendor, "kind": "vendor",
                                      "refs": [], "basis": board_basis(ws)}
    return out


def chosen_vendor(ws: Path) -> str | None:
    """state.json's "vendor" (vendor.py choose), else None: no vendor fact
    is expected of a board that never chose one."""
    try:
        data = json.loads((Path(ws) / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("vendor") if isinstance(data, dict) else None
    return str(v) if v else None


def board_pcb(ws: Path) -> Path | None:
    kdir = Path(ws) / "kicad"
    pcbs = sorted(kdir.glob("*.kicad_pcb")) if kdir.is_dir() else []
    return pcbs[0] if len(pcbs) == 1 else None


def board_basis(ws: Path) -> str | None:
    """The board file a vendor fit was judged on (sha256, uuids aside)."""
    pcb = board_pcb(ws)
    if pcb is None:
        return None
    import statelib
    return statelib.hash_artifact(pcb, statelib.norm_for_path(pcb))


# ----------------------------------------------------------------- writing

def put(ws: Path, facts: list[dict], by: str) -> Path | None:
    """Merge facts (subject, kind, status, value, source[, lcsc, refs]) into
    the ledger, stamping checked_at and by; also lists every expected fact
    not yet recorded as unknown, so the file itself shows the gaps."""
    if not writable(ws):
        return None
    data = load(ws)
    ts = now()
    for f in facts:
        if f.get("kind") not in KINDS or f.get("status") not in STATUSES:
            raise ValueError(f"bad fact {f!r}: kind one of {KINDS}, status "
                             f"one of {STATUSES}")
        ent = {k: f[k] for k in ("subject", "kind", "status", "value",
                                 "source", "lcsc", "refs", "basis", "forced",
                                 "forced_over", "rated_from")
               if k in f}
        ent.setdefault("value", "")
        ent.setdefault("source", "")
        ent["checked_at"] = f.get("checked_at") or ts
        ent["by"] = by
        data["facts"][key(f["subject"], f["kind"])] = ent
    for k, e in expected(ws).items():
        data["facts"].setdefault(k, {**e, "status": "unknown", "value": "",
                                     "source": "", "checked_at": None,
                                     "by": None})
    data["schema"] = SCHEMA
    data["updated"] = ts
    p = path(ws)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True,
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def stock_facts(result: dict) -> list[dict]:
    """stock_check's rows -> stock, part_match and lifecycle facts."""
    out = []
    for r in result.get("rows") or []:
        code = r.get("lcsc") or f"(no LCSC: {(r.get('refs') or ['?'])[0]})"
        base = {"subject": code, "lcsc": code, "refs": r.get("refs") or [],
                "source": STOCK_SOURCE, "checked_at": r.get("checked_at")}
        jlc = r.get("jlc")
        if not r.get("lcsc"):                  # no code: the one item
            out.append({**base, "kind": "stock", "status": "unknown",
                        "value": r.get("why") or "no LCSC code"})
            continue
        if r["status"] == "unknown" and jlc is None:
            for kind in PART_KINDS:
                out.append({**base, "kind": kind, "status": "unknown",
                            "value": r.get("why", "")})
            continue
        if jlc is None:                                   # not_found
            for kind in PART_KINDS:
                out.append({**base, "kind": kind, "status": "failed",
                            "value": r.get("why", "")})
            continue
        avail, need = r.get("available"), r.get("need")
        out.append({**base, "kind": "stock",
                    "status": ("unknown" if avail is None else
                               "verified" if avail >= need else "failed"),
                    "value": f"JLC can sell {avail}, build needs {need}"
                             f" (JLC lists {jlc.get('stock')}, LCSC "
                             f"{(r.get('lcsc_record') or {}).get('stock')})"})
        m = r.get("match") or {}
        out.append({**base, "kind": "part_match",
                    "status": {True: "verified", False: "failed"}.get(
                        m.get("ok"), "unknown"),
                    "value": m.get("why", "")})
        life = "verified" if jlc.get("for_sale") else "failed"
        out.append({**base, "kind": "lifecycle", "status": life,
                    "value": ("JLC sells it" if life == "verified"
                              else r.get("why", ""))})
    return out


def record_stock(ws: Path, result: dict) -> Path | None:
    """Record stock_check's rows, then drop the facts of anything no longer
    on the board (placeholders that gained a code or left the BOM, a vendor
    no longer chosen)."""
    rows = []
    for r in result.get("rows") or []:
        if not r.get("lcsc"):
            refs = codeless_needed(ws, r.get("refs") or [])
            if r.get("refs") and not refs:
                continue                       # holes, test points, DNP
            r = {**r, "refs": refs or r.get("refs")}
        rows.append(r)
    out = put(ws, stock_facts({**result, "rows": rows}), "stock_check")
    if out is not None:
        prune(ws, apply=True)
    return out


# ----------------------------------------------------------------- reading

def effective(fact: dict | None, exp: dict | None, at=None) -> dict:
    """The fact as it reads now: stale and other-part facts read unknown."""
    at = at or _dt.datetime.now(_dt.timezone.utc)
    if fact is None:
        return {**(exp or {}), "status": "unknown", "value": "",
                "source": "", "checked_at": None, "why": "never checked"}
    out = dict(fact)
    if out.get("status") == "unknown":
        out.setdefault("why", out.get("value") or "never checked")
        return out
    if exp and exp.get("lcsc") and fact.get("lcsc") and \
            exp["lcsc"] != fact["lcsc"]:
        out.update(status="unknown",
                   why=f"checked for {fact['lcsc']}; the board now has "
                       f"{exp['lcsc']}")
    elif exp and exp.get("basis") and fact.get("basis") != exp["basis"]:
        out.update(status="unknown",
                   why="fitted to an older board file - rerun vendor.py fit")
    elif fact.get("kind") == "stock":
        t = _parse(fact.get("checked_at"))
        age = (at - t).total_seconds() / 3600 if t else None
        if age is None or age > STOCK_MAX_AGE_H:
            out.update(status="unknown",
                       why=f"stale: checked {fact.get('checked_at')}, stock "
                           f"is only good for {STOCK_MAX_AGE_H:.0f} h - "
                           "rerun stock_check.py")
    if out.get("status") == "verified" and out.get("kind") == "rating" \
            and out.get("by") == "check_ratings" \
            and str(out.get("source") or "").startswith(ATTRIBUTE_SOURCE):
        out.update(status="listed", rated_from="distributor",
                   value="rated from parts.json, no datasheet read",
                   why="written before #781 called this verified; rerun "
                       "check_ratings")
    return out


def present_subjects(ws: Path) -> set[str]:
    """Every refdes, LCSC code and vendor the board still has: the subjects a
    fact may be about. DNP and unbought lines count (they are on the board)."""
    ws = Path(ws)
    out = {e["subject"] for e in expected(ws).values()}
    for ln in _parts(ws):
        out.update(_refs(ln))
        code = str(ln.get("lcsc") or "").strip()
        if code:
            out.add(code)
    for code, refs in (_bom_codes(ws) or {}).items():
        out.add(code)
        out.update(refs)
    return out


def orphans(ws: Path) -> dict[str, dict]:
    """key -> fact, for facts whose subject (refdes, LCSC code or vendor) is
    no longer on the board: a swapped or removed part's leftovers."""
    present = present_subjects(ws)
    exp = expected(ws)
    out = {}
    for k, f in load(ws)["facts"].items():
        subj = f.get("subject") or k.rsplit("/", 1)[0]
        # a hole, test point or fiducial's unknown rating/footprint fact from
        # before #789: the part is present, the fact is no longer expected
        stale = (k not in exp and NO_CODE_REF.match(subj)
                 and f.get("kind") in REF_KINDS and f.get("status") == "unknown")
        if subj not in present or stale:
            out[k] = f
    return out


def prune(ws: Path, apply: bool = False) -> list[str]:
    """Remove the orphaned facts (only written when apply and writable)."""
    gone = sorted(orphans(ws))
    if apply and gone and writable(ws):
        data = load(ws)
        for k in gone:
            data["facts"].pop(k, None)
        data["updated"] = now()
        p = path(ws)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True,
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    return gone


def report(ws: Path, at=None) -> dict:
    """Every expected fact as it reads now, the gaps, and `ready`."""
    ws = Path(ws)
    data = load(ws)
    exp = expected(ws)
    facts = data["facts"]
    rows = []
    for k in sorted(set(exp) | set(facts)):
        if k not in exp:
            continue             # a fact about something no longer on board
        rows.append({"key": k, **effective(facts.get(k), exp[k], at)})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_kind.setdefault(r["kind"], {})
        d[r["status"]] = d.get(r["status"], 0) + 1
    failed = [r for r in rows if r["status"] == "failed"]
    unknown = [r for r in rows if r["status"] == "unknown"]
    listed = [r for r in rows if r["status"] == "listed"]
    orphaned = [{"key": k, "subject": f.get("subject"), "kind": f.get("kind"),
                 "status": f.get("status")}
                for k, f in sorted(orphans(ws).items())]
    return {"ledger": str(path(ws)), "exists": path(ws).is_file(),
            "orphaned": orphaned,
            "ready": not failed and not unknown, "counts": counts,
            "by_kind": by_kind, "failed": failed, "unknown": unknown,
            "listed": listed, "facts": rows}


def blockers(ws: Path, at=None, limit: int = 12) -> list[str]:
    """Release problems: every critical fact failed or unknown, summarised.
    A listed one is not a problem: see the module docstring."""
    rep = report(ws, at)
    out = []
    for status in ("failed", "unknown"):
        rows = rep[status]
        if not rows:
            continue
        by_kind: dict[str, list[str]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r["subject"])
        for kind, subs in sorted(by_kind.items()):
            more = f" +{len(subs) - limit} more" if len(subs) > limit else ""
            out.append(f"ledger: {len(subs)} {kind} fact(s) {status} - "
                       f"{', '.join(subs[:limit])}{more} (ledger.py report "
                       "--workspace <ws>)")
    return out
