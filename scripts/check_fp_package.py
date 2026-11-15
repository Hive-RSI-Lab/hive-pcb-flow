"""check_fp_package.py - every part's footprint against the package of the
part it names (P8 verify suite).

The misses (docs/miss-taxonomy.md, class 1): a 1N5819 drawn on SOD-123F
when the part named is a DO-41 or a SOD-123 (flexgrid-v3-1), a capacitor
whose JLC number is an 0402 part on an 0603 footprint (flexgrid-v3-2), an
SOIC-8 150 mil flash on the 208 mil footprint (ice1usb-r01-1). Each part on
the board names a part (an LCSC/JLC number, an MPN, or a Value that is a
part number); that part's record has a package; the package name and the
footprint name each say a mount (surface or through hole), a family (SOIC,
SOD-123F, SOT-23, QFN, chip size...), a pin count, a pitch and a body.
lib/pkgname.py reads both and compares only what both state, so a name it
can't read is "not judged", never a hit.

Where the part's record comes from, first found wins:
  1. an LCSC field on the footprint (`LCSC`, `LCSC Part`, `LCSC Part #`) or
     a JLC one (`JLCPN`, `JLC`, `JLCPCB Part #`): the code's package from
     parts.json beside the board, then reference/lcsc_packages.json, then
     (--live) JLC's record;
  2. an MPN field (`MPN`, `Manufacturer_Part_Number`, `MFR PN`, ...), or for
     a D, Q or U whose Value is a part number (`1N5819`, `W25Q80DVSNIG`),
     that Value: first its ordering code, read from the maker's own table
     (reference/mpn_packages.yaml via lib/mpn_decode.py, offline too:
     SN74HC595PW is TSSOP-16, ATTINY85-20SU the 208 mil SOIC,
     STM32F103C8T6 LQFP-48 7x7, W25Q80DVSNIG the 150 mil SOIC-8), else
     (--live) JLC's search for rows whose model is that exact name. A name
     those rows give in several packages, or a generic JEDEC one (1N4007,
     1N5819: registered DO-41, sold in SMA and SOD-123FL too), is judged
     against each: it passes when the footprint fits exactly one, is a
     warning when it fits none of them (live; offline only the registered
     package is known, so a miss there is "not judged"), and is "not
     judged" when it fits two or more. A Manufacturer field naming another
     maker than the table's leaves the code undecoded, and a Value that
     ends at a code starting a longer one (TPS2552D: TI's D, but DBV too)
     is taken as cut short, not judged. A Value-named part is reported as
     a warning: the designer may mean a variant the Value does not spell
     (1N5819W).
  A code with no record, or a record whose package name is not read, falls
  back to the decoded MPN field, then to the record's own model.
  Neither read: "not judged", never a guess.
--live answers are cached in --cache (default ~/.cache/hwde/fp_package) so
a sweep asks JLC once per code or name.

The pin count on the footprint side is the one its name gives (SOT-23-5,
QFN-32-1EP is 32), else its numbered pads (one more than the part's is an
exposed pad). The mount on the footprint side is its pads': SMD pads only is
surface mount, plated pins only is through hole.

A passive (R, C, L) bought by LCSC/JLC number has its value checked too:
the record's description gives the capacitance, resistance or inductance,
and a Value more than 2 % off is a hit (flexgrid V3 C9: "100 uF" bought as
C12530, a 2.2 uF part).

Every C and R on a chip-size footprint, part number or not, has its stated
value held to its case (reference/case_limits.yaml, the makers' lineup
charts): a 22 uF 0402 or a 1 W 0603 cannot be bought. Its `case_limits_judged`
counts them.

Kinds:
  footprint_package_mismatch  the footprint and the named part's package
                              disagree on mount, family, pins, pitch or
                              body width
  lcsc_value_mismatch         the Value and the LCSC/JLC record's value
                              disagree
  passive_case_limit          a C or R states more than its chip case holds:
                              a capacitance over 2.5x the largest the Yageo
                              and KEMET charts list for the case, voltage
                              and dielectric (X5R, X7R; C0G or unstated is
                              held to any dielectric's), or a power over 2x Yageo RC_L's
                              rating (lib/case_limits.py; a warning)

The report's `judged` counts parts both sides could be read for; `not_judged`
says why each other part wasn't.

CLI: --pcb board.kicad_pcb [--parts parts.json] [--live] [--cache DIR]
     [--out report.json]
     exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import bom_cpl  # noqa: E402  (is_lcsc_field)
import case_limits  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import fpfix  # noqa: E402
import lcsc_packages  # noqa: E402
import mpn_decode  # noqa: E402
import pkgname  # noqa: E402
import stocklib  # noqa: E402

SCRIPT = "check_fp_package"
CACHE = Path.home() / ".cache" / "hwde" / "fp_package"

_JLC_FIELD = re.compile(r"^JLC(?:PCB)?(?:\s*PART)?\s*(?:PN|#|NO\.?|NUMBER)?$",
                        re.I)
_MPN_FIELD = re.compile(
    r"^(?:MPN#?|MFR\.?\s*(?:PART\s*)?(?:#|PN|NO\.?|NUMBER)?|MF?PN|"
    r"MANUFACTURER[_\s]*PART[_\s]*(?:NUMBER|#|NO\.?)?|PART\s*NUMBER|PN)$",
    re.I)
_MAKER_FIELD = re.compile(r"^(?:MANUFACTURER|MFR\.?|MFG|MAKER|VENDOR)"
                          r"(?:[_\s]*NAME)?$", re.I)
_CODE = re.compile(r"^C\d{3,9}$")
# a Value that is a part number: letters and digits, at least 5 long, with
# both a letter and two digits, and no value unit standing alone
_PARTNO = re.compile(r"^(?=.*[A-Z])(?=(?:.*\d){2})[A-Z0-9][A-Z0-9\-./+#]{4,}$")


def is_jlc_field(name: str) -> bool:
    return bool(_JLC_FIELD.match(name.strip()))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).upper()


# ------------------------------------------------------------------ board

def footprints(pcb: Path) -> list[dict]:
    """Every footprint: ref, fpid, value, props, pad count, pad mount,
    attrs, position and layer."""
    text = pcb.read_text(encoding="utf-8")
    out = []
    for head, s, e in fpfix.top_level_nodes(text):
        if head != "footprint":
            continue
        root = fpfix._parse(text[s:e])
        props: dict[str, str] = {}
        attrs: set[str] = set()
        numbers: set[str] = set()
        kinds: set[str] = set()
        for c in root[1:]:
            if not isinstance(c, list) or not c:
                continue
            h = str(fpfix._tok(c[0]))
            if h == "property" and len(c) >= 3:
                props[str(fpfix._tok(c[1]))] = str(fpfix._tok(c[2]))
            elif h == "attr":
                attrs |= {str(fpfix._tok(a)) for a in c[1:]}
            elif h == "pad" and len(c) >= 3:
                num, kind = str(fpfix._tok(c[1])), str(fpfix._tok(c[2]))
                if kind in ("smd", "thru_hole") and num:
                    numbers.add(num)
                    kinds.add(kind)
        at = fpfix._nums(fpfix._kid(root, "at"))
        layer = fpfix._kid(root, "layer")
        out.append({
            "ref": props.get("Reference", "?"),
            "fpid": str(fpfix._tok(root[1])),
            "value": props.get("Value", ""),
            "props": props, "attrs": attrs, "pads": len(numbers),
            "pad_mount": ({"smd"} == kinds and "smd")
            or ({"thru_hole"} == kinds and "tht") or None,
            "at": at[:2] if len(at) >= 2 else None,
            "layer": str(fpfix._tok(layer[1])) if layer is not None else None,
        })
    return out


def part_number(fp: dict) -> tuple[str | None, str | None, str]:
    """(LCSC code, MPN, where it was read). The code wins when both are
    there; a Value is read as an MPN only on a D, Q or U."""
    props = fp["props"]
    code = next((v.strip().upper() for k, v in props.items()
                 if bom_cpl.is_lcsc_field(k) and _CODE.match(v.strip().upper())),
                None)
    if code:
        return code, None, "LCSC field"
    code = next((v.strip().upper() for k, v in props.items()
                 if is_jlc_field(k) and _CODE.match(v.strip().upper())), None)
    if code:
        return code, None, "JLC field"
    mpn = next((v.strip() for k, v in props.items()
                if _MPN_FIELD.match(k.strip()) and v.strip()
                and v.strip() not in ("~", "-")), None)
    if mpn:
        return None, mpn, "MPN field"
    val = fp["value"].strip()
    prefix = re.match(r"[A-Z]+", fp["ref"].upper())
    if prefix and prefix.group(0) in ("D", "Q", "U", "IC") \
            and _PARTNO.match(val.upper()) and engcalc.parse_value(val) is None:
        return None, val, "Value"
    return None, None, ""


# ------------------------------------------------------------------ records

def parts_json_records(path: Path | None) -> tuple[dict, dict]:
    """(LCSC code -> record, ref -> (code, record)) from parts.json, a
    record being {package, description, model, source}. A row's `refdes`
    says which parts it buys, so a ref it lists is judged against it."""
    codes: dict[str, dict] = {}
    refs: dict[str, tuple[str, dict]] = {}
    if not path or not path.is_file():
        return codes, refs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return codes, refs
    rows = data.get("parts") if isinstance(data, dict) else data
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("lcsc") and r.get("package"):
            code = str(r["lcsc"]).strip().upper()
            rec = {"package": str(r["package"]),
                   "description": str(r.get("description") or ""),
                   "model": r.get("mpn") or None, "source": "parts.json"}
            codes[code] = rec
            for ref in r.get("refdes") or []:
                refs[str(ref)] = (code, rec)
    return codes, refs


def find_parts_json(pcb: Path) -> Path | None:
    for p in (pcb.parent / "parts.json", pcb.parent / "parts" / "parts.json",
              pcb.parent.parent / "parts" / "parts.json"):
        if p.is_file():
            return p
    return None


class Records:
    """Where a part's package and value are looked up, with the --live
    answers cached on disk."""

    def __init__(self, local: dict, table: dict, live: bool,
                 cache: Path | None, fetch=None, refs: dict | None = None):
        self.local, self.table, self.live = local, table, live
        self.refs = refs or {}
        self.cache, self.fetch = cache, fetch or stocklib.http_fetch
        self.errors: list[str] = []

    def _cached(self, key: str, ask):
        f = self.cache / f"{re.sub(r'[^A-Za-z0-9._-]', '_', key)}.json" \
            if self.cache else None
        if f and f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))["rows"]
            except (OSError, ValueError, KeyError):
                pass
        rows = ask()
        if f:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"key": key, "read": time.strftime(
                "%Y-%m-%d"), "rows": rows}), encoding="utf-8")
        return rows

    def _search(self, keyword: str) -> list[dict]:
        body = self.fetch("POST", stocklib.JLC_SEARCH,
                          {"keyword": keyword, "currentPage": 1,
                           "pageSize": 50})
        try:
            rows = body["data"]["componentPageInfo"]["list"] or []
        except (KeyError, TypeError) as exc:
            if isinstance(body, dict) and body.get("code") not in (200, None):
                raise stocklib.SourceError(
                    f"JLC search answered code {body.get('code')}") from exc
            rows = []
        return [{"code": r.get("componentCode") or "",
                 "model": r.get("componentModelEn") or "",
                 "package": r.get("componentSpecificationEn") or "",
                 "description": r.get("describe") or ""} for r in rows]

    def by_code(self, code: str) -> dict | None:
        """{package, description, model, source} or None (with why in
        self.errors when JLC could not be asked)."""
        if code in self.local:
            return self.local[code]
        live = None
        if self.live:
            try:
                rows = self._cached(code, lambda: self._search(code))
                live = next((r for r in rows if r["code"].upper() == code),
                            None)
            except stocklib.SourceError as exc:
                self.errors.append(f"{code}: {exc}")
        if live:
            return {**live, "source": "JLC's record"}
        ent = self.table.get(code)
        if isinstance(ent, dict) and ent.get("package"):
            return {"package": ent["package"], "description": "",
                    "model": ent.get("model"),
                    "source": "reference/lcsc_packages.json"}
        return None

    def by_mpn(self, mpn: str, maker: str | None = None
               ) -> tuple[dict | None, str]:
        """(record, why not) for an exact model name: its ordering code
        read from the maker's own table (lib/mpn_decode.py), else (--live)
        the JLC rows whose model is the name. Rows that disagree on the
        package, or a generic JEDEC name (an `open` table entry: 1N4007 is
        registered DO-41 but sold in SMA and SOD-123FL too), give a record
        with `candidates`, one per package, for check() to judge."""
        dec = decoded(mpn, maker)
        if dec and not dec.get("open"):
            return dec, ""
        if not self.live:
            return (dec, "") if dec else (
                None, "an MPN is looked up only with --live")
        try:
            rows = self._cached("mpn-" + _norm(mpn),
                                lambda: self._search(mpn))
        except stocklib.SourceError as exc:
            self.errors.append(f"{mpn}: {exc}")
            return (dec, "") if dec else (
                None, f"JLC could not be asked ({exc})")
        exact = [r for r in rows if _norm(r["model"]) == _norm(mpn)
                 and r["package"]]
        if not exact:
            return (dec, "") if dec else (
                None, "JLC lists no part of that exact name")
        seen = {}
        for r in exact:
            d = pkgname.describe(r["package"])
            key = (d["mount"], d["family"], d["chip"], d["pins"])
            seen.setdefault(key, []).append(r)
        if len(seen) == 1 and not dec:
            if next(iter(seen))[1] is None and next(iter(seen))[2] is None:
                return None, f"package {exact[0]['package']!r} not read"
            r = exact[0]
            codes = ", ".join(x["code"] for x in exact[:4])
            return {**r, "source": f"JLC's parts named {mpn} ({codes})",
                    "rows": len(exact)}, ""
        cands = [{"package": rs[0]["package"], "desc": None,
                  "source": "JLC's " + ", ".join(x["code"] for x in rs[:3])}
                 for rs in seen.values()]
        if dec:
            jd = dec["candidates"][0]["desc"]
            if (jd["mount"], jd["family"], jd["chip"], jd["pins"]) \
                    not in seen:
                cands = [dec["candidates"][0]] + cands
        pk = sorted({c["package"] for c in cands})
        return {"package": " or ".join(pk), "description": "", "model": None,
                "source": f"JLC's parts named {mpn}"
                          + (f" and {dec['maker']}" if dec else ""),
                "candidates": cands, "open": True, "rows": len(exact)}, ""


def _candidates(rec: dict, foot: dict, pads) -> tuple[str, str]:
    """A name sold in several packages against one footprint: ("ok", "")
    when the footprint fits exactly one, ("hit", why) when it fits none
    and every package was read (and, for a generic name offline, never:
    other makers' bodies are not known), else ("skip", why)."""
    if not (foot["mount"] or foot["family"]):
        return "skip", "the footprint's name is not read"
    fits, unread, why = [], [], []
    for c in rec["candidates"]:
        part = c.get("desc") or pkgname.describe(c["package"])
        if not (part["family"] or part["chip"]):
            unread.append(c["package"])
            continue
        bad = pkgname.compare(part, foot, pads)
        if bad:
            why.append(f"{c['package']}: {bad[0]}")
        else:
            fits.append(c["package"])
    if len(fits) == 1 and not unread:
        return "ok", ""
    if len(fits) > 1:
        return "skip", "the name is sold in several packages and the " \
            "footprint fits more than one"
    if fits:
        return "skip", "the name is sold in several packages, some not read"
    if unread:
        return "skip", "the name is sold in several packages, some not read"
    if rec.get("offline"):
        return "skip", "a generic name sold in other packages too " \
            "(--live asks JLC which)"
    return "hit", "; ".join(why)


def maker_of(fp: dict) -> str | None:
    return next((v.strip() for k, v in fp["props"].items()
                 if _MAKER_FIELD.match(k.strip()) and v.strip()), None)


def decoded(mpn: str | None, maker: str | None = None) -> dict | None:
    """The record an MPN's ordering code gives (reference/
    mpn_packages.yaml), or None. A Manufacturer field naming another maker
    than the table's (a 74HC595D from Toshiba) is not decoded: the code
    letters are that maker's own."""
    if not mpn:
        return None
    dec = mpn_decode.decode(mpn)
    if not dec:
        return None
    if dec.get("open"):
        # a generic JEDEC name: any maker's, its registered package one
        # candidate (offline the only one known)
        cand = {"package": dec["package"], "desc": dec["desc"],
                "source": dec["source"]}
        return {"package": dec["package"], "description": "", "model": None,
                "source": dec["source"], "maker": dec["maker"],
                "candidates": [cand], "open": True, "offline": True,
                "rows": 2}
    if maker:
        words = {w for w in re.split(r"[^a-z]+", dec["maker"].split()[0]
                                     .lower()) if len(w) > 2}
        words |= {"atmel"} if "microchip" in words else set()
        words |= {"fairchild"} if "onsemi" in words else set()
        words |= {"siliconix"} if "vishay" in words else set()
        mk = maker.lower()
        if words and not any(w in mk for w in words) and not (
                "texas" in words and re.search(r"\bti\b", mk)) and not (
                "onsemi" in words and "on semi" in mk):
            return None
    return {"package": dec["package"], "desc": dec["desc"],
            "description": "", "model": None, "source": dec["source"],
            "maker": dec["maker"], "decoded": True, "cut": dec["cut"],
            "rows": 2}


def _mpn_field(fp: dict) -> str | None:
    return next((v.strip() for k, v in fp["props"].items()
                 if _MPN_FIELD.match(k.strip()) and v.strip()
                 and v.strip() not in ("~", "-")), None)


# ------------------------------------------------------------------ values

_DESC_VAL = {
    "C": re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([pnuµμm]?)F(?![a-zA-Z])"),
    "R": re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([mkKM]?)\s*(?:Ω|ohms?\b)",
                    re.I),
    "L": re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([pnuµμm]?)H(?![a-zA-Z])"),
}
_MULT = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
         "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}


def record_value(kind: str, description: str) -> float | None:
    m = _DESC_VAL[kind].search(description or "")
    if not m:
        return None
    return float(m.group(1)) * _MULT[m.group(2)]


# ------------------------------------------------------------------ check

def _reads(package: str) -> bool:
    d = pkgname.describe(package or "")
    return bool(d["family"] or d["chip"])


def check(pcb: Path, recs: Records) -> tuple[list[dict], dict]:
    viols: list[dict] = []
    judged: list[dict] = []
    not_judged: dict[str, int] = {}
    values = 0
    limits = 0

    def skip(why: str) -> None:
        not_judged[why] = not_judged.get(why, 0) + 1

    for fp in footprints(pcb):
        ref = fp["ref"]
        if ref.startswith("#") or not fp["pads"] or {
                "dnp", "board_only", "exclude_from_bom", "virtual"} & fp["attrs"]:
            continue
        code, mpn, where = part_number(fp)
        if ref in recs.refs:  # parts.json says what this ref buys
            code, mpn, where = recs.refs[ref][0], None, "parts.json row"
        kind = (re.match(r"[A-Z]+", ref.upper()) or [""])[0]
        if kind in ("C", "R"):
            cl = case_limits.judge(kind, fp["value"], fp["fpid"], fp["props"])
            if cl:
                limits += 1
                if not cl["ok"]:
                    unit = cl["unit"]
                    viols.append(checklib.violation(
                        SCRIPT, "warning", fp["at"], fp["layer"], None, [ref],
                        f"{ref} is {cl['what']} ({fp['value']!r} on "
                        f"{fp['fpid']}), but the largest "
                        f"{'capacitor' if kind == 'C' else 'rated power'} "
                        f"the makers' charts give for that case is "
                        f"{case_limits._si(cl['limit'], unit)} "
                        f"({'; '.join(cl['cites'][:3])}; "
                        f"reference/case_limits.yaml). Fix: the case the "
                        f"part's datasheet gives, or a value that fits "
                        f"{cl['case']}",
                        SCRIPT, kind="passive_case_limit", case=cl["case"],
                        value=fp["value"], stated=cl["stated"],
                        limit=cl["limit"], unit=unit))
        if not code and not mpn:
            skip("no part number on the footprint")
            continue
        if code:
            rec, why = recs.by_code(code), "no record of the code" + (
                "" if recs.live else " (--live asks JLC)")
        else:
            rec, why = recs.by_mpn(mpn, maker_of(fp))
            if rec and rec.get("cut") and where == "Value":
                # #1211: TPS2552D is TPS2552DBVR cut short, not TI's D SOIC
                skip("the Value may be an ordering code cut short")
                continue
        pkg = rec
        if code and (not rec or not _reads(rec["package"])):
            # no record of the code, or one whose package name is not
            # read: the MPN on the part, else the record's own model, may
            # still decode from its ordering code
            for alt, src in ((_mpn_field(fp), "MPN field"),
                             ((rec or {}).get("model"), "record's model")):
                dec = decoded(alt, maker_of(fp))
                if dec and not dec.get("open"):
                    pkg, mpn = dec, alt
                    where = f"{where}; package from the {src} {alt}"
                    break
        if not rec and not pkg:
            skip(why)
            continue
        rec = rec or pkg
        foot = pkgname.describe(fp["fpid"])
        if fp["pad_mount"]:
            foot["mount"] = fp["pad_mount"]
        if pkg.get("candidates"):
            verdict = _candidates(pkg, foot, fp["pads"])
            if verdict[0] == "skip":
                skip(verdict[1])
                continue
            judged.append({"ref": ref, "named": mpn, "from": where,
                           "package": pkg["package"],
                           "footprint": fp["fpid"],
                           "ok": verdict[0] == "ok"})
            if verdict[0] == "ok":
                continue
            viols.append(checklib.violation(
                SCRIPT, "warning", fp["at"], fp["layer"], None, [ref],
                f"{ref} is {mpn}, a part sold as {pkg['package']} "
                f"({pkg['source']}; the part number from its {where}), but "
                f"its footprint is {fp['fpid']}, which fits none of them: "
                f"{verdict[1]}. Fix: the footprint of the package bought, "
                "or put the LCSC number or maker's order code of a part "
                "that fits these pads on it",
                SCRIPT, kind="footprint_package_mismatch",
                footprint=fp["fpid"], package=pkg["package"], part=mpn,
                part_from=where, reasons=[verdict[1]]))
            continue
        part = pkg.get("desc") or pkgname.describe(pkg["package"])
        named = f"{code or mpn} ({rec['model']})" if rec.get("model") \
            and _norm(rec["model"]) != _norm(code or mpn) else (code or mpn)
        warn = where == "Value"
        # the value of a passive bought by number
        if code and kind in _DESC_VAL:
            # "2.2 uF": engcalc reads the number alone without the unit
            # and the Greek mu (U+03BC) is read as the micro sign
            want = engcalc.parse_value(re.sub(
                r"(\d)\s+(?=[pnuµmkKMGR]|[Oo]hm|Ω)", r"\1",
                fp["value"].replace("\u03bc", "\u00b5")))
            got = record_value(kind, rec.get("description", ""))
            if want and got:
                values += 1
                if abs(want - got) > 0.02 * max(want, got):
                    viols.append(checklib.violation(
                        SCRIPT, "error", fp["at"], fp["layer"], None, [ref],
                        f"{ref}'s Value is {fp['value']!r} but it is bought "
                        f"as {named}, {_fmt(got, kind)} ({rec['source']}; "
                        f"its description: {rec['description'][:80]!r}). "
                        f"Fix the number or the Value",
                        SCRIPT, kind="lcsc_value_mismatch", lcsc=code,
                        value=fp["value"], record_value=got))
        if not (part["mount"] or part["family"]):
            skip("the part's package name is not read")
            continue
        if not (foot["mount"] or foot["family"]):
            skip("the footprint's name is not read")
            continue
        if not (part["family"] or part["chip"]) \
                and not (foot["family"] or foot["chip"]):
            # a mount alone, from an SMD or PLUGIN word in a connector's
            # name, is too weak a reading to fail a part on
            skip("the package names give only a mount")
            continue
        if warn and code is None and rec.get("rows", 2) < 2 \
                and part["mount"] and foot["mount"] \
                and part["mount"] != foot["mount"]:
            # a Value such as BAT85 names a part sold in more than one
            # body; one JLC row in SMD says nothing of a THT footprint
            skip("the Value names a part JLC has once, in another mount")
            continue
        same = not pkg.get("decoded") and \
            _norm(pkg["package"]) == _norm(fp["fpid"].split(":")[-1])
        why_not = [] if same else pkgname.compare(part, foot, fp["pads"])
        judged.append({"ref": ref, "named": code or mpn, "from": where,
                       "package": pkg["package"], "footprint": fp["fpid"],
                       "ok": not why_not})
        if not why_not:
            continue
        viols.append(checklib.violation(
            SCRIPT, "warning" if warn else "error", fp["at"], fp["layer"],
            None, [ref],
            f"{ref} is {named}, a {pkg['package']} part ({pkg['source']}; "
            f"the part number from its {where}), but its footprint is "
            f"{fp['fpid']}: {'; '.join(why_not)}"
            + (". The Value names that part; if a variant is meant, put its "
               "LCSC number or MPN on the part" if warn else "")
            + ". Fix: the footprint the part's datasheet gives, or the part "
              "that fits these pads",
            SCRIPT, kind="footprint_package_mismatch", footprint=fp["fpid"],
            package=pkg["package"], part=code or mpn, part_from=where,
            reasons=why_not))
    return viols, {"judged": len(judged), "not_judged": not_judged,
                   "values_judged": values, "case_limits_judged": limits,
                   "parts": judged,
                   "lookup_errors": recs.errors[:20]}


def _fmt(v: float, kind: str) -> str:
    unit = {"C": "F", "R": "ohm", "L": "H"}[kind]
    for m, p in ((1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "u"),
                 (1e-9, "n"), (1e-12, "p")):
        if v >= m * 0.999:
            return f"{v / m:g} {p}{unit}"
    return f"{v:g} {unit}"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--parts", help="parts.json (default: found beside the "
                    "board)")
    ap.add_argument("--live", action="store_true",
                    help="ask JLC for codes and MPNs the files don't give")
    ap.add_argument("--cache", default=str(CACHE),
                    help="where --live answers are kept ('' for none)")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    parts = Path(args.parts) if args.parts else find_parts_json(pcb)
    if parts and parts.is_dir():
        parts = parts / "parts.json"
    codes, refs = parts_json_records(parts)
    recs = Records(codes, lcsc_packages.load(), args.live,
                   Path(args.cache) if args.cache else None, refs=refs)
    viols, facts = check(pcb, recs)
    return checklib.report(SCRIPT, pcb, viols, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
