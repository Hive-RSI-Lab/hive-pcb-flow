"""case_limits.py - the largest capacitor or resistor a chip case holds.

reference/case_limits.yaml holds the makers' lineup charts, each row cited
to its page: per case, dielectric and rated voltage the largest capacitance
Yageo and KEMET list, and per case the rated power of Yageo's RC_L resistors
(its 2x option where it has one).

cap_limit(case, dielectric, volts) -> (farads, cites) or None
  The largest C over the rows whose voltage column is at or above v*, the
  largest column at or below the stated voltage: a 35 V part in a case
  charted at 25 V and 50 V is held to the 25 V column. With no voltage, or
  one below every column, every column counts; with no dielectric, the
  larger of X5R, X7R and C0G. None when the case has no row.
res_limit(case) -> (watts, cites) or None
judge(kind, value, footprint, props) -> None (not judged) or a verdict
  A C or R on a chip-size footprint, read from its Value and the props
  that name a voltage, power or dielectric. A dielectric named outside
  X5R/X7R/C0G, an electrolytic, tantalum or film part, or a part with no
  stated power (R) is not judged.

MARGIN: a capacitor is flagged only above MARGIN (2.5) x the limit, as two
makers' charts are not the whole market (Murata, TDK and Samsung may list
one E3 step more in some cells: 10 uF X5R in 0201); a resistor only above R_MARGIN (2) x its
case's rating, as anti-surge and thick-power series (ROHM ESR, Vishay RCS,
Panasonic ERJ-P) rate one step above RC_L's 2x option.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

TABLE = Path(__file__).resolve().parents[2] / "reference" / "case_limits.yaml"
DIELECTRICS = ("X5R", "X7R", "C0G")
MARGIN = 2.5
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6}


def _farads(v: str) -> float:
    v = str(v)
    return float(v[:-1]) * _SI[v[-1]] if v[-1] in _SI else float(v)


@lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    """{'caps': [(case, dielectric, volts, farads, cite)], 'res': {case:
    [(watts, cite)]}}, one cap tuple per voltage column of a row."""
    data = yaml.safe_load(Path(path or TABLE).read_text(encoding="utf-8"))
    caps = []
    for r in data.get("capacitors") or []:
        for v in r["v"]:
            caps.append((str(r["case"]), r["dielectric"], float(v),
                         _farads(r["value"]), r["cite"][0]))
    res: dict[str, list] = {}
    for r in data.get("resistors") or []:
        res.setdefault(str(r["case"]), []).append((float(r["power"]),
                                                   r["cite"][0]))
    return {"caps": caps, "res": res}


def _ref(c: dict) -> str:
    return f"{c['doc']} p{c['page']}" + (f" {c['table']}" if c.get("table")
                                         else "")


def cap_limit(case: str, dielectric: str | None = None,
              volts: float | None = None, path: str | None = None):
    rows = [r for r in load(path)["caps"] if r[0] == case
            and (dielectric is None or r[1] == dielectric)]
    if not rows:
        return None
    cols = sorted({r[2] for r in rows})
    if volts is not None and volts >= cols[0]:
        vstar = max(c for c in cols if c <= volts)
        rows = [r for r in rows if r[2] >= vstar]
    best = max(r[3] for r in rows)
    cites = []
    for r in rows:
        if r[3] == best and _ref(r[4]) not in cites:
            cites.append(_ref(r[4]))
    return best, cites


def res_limit(case: str, path: str | None = None):
    rows = load(path)["res"].get(case)
    if not rows:
        return None
    best = max(w for w, _ in rows)
    return best, [_ref(c) for w, c in rows if w == best]


R_MARGIN = 2.0
_PROP_KEY = re.compile(r"volt|rating|dielectric|power|watt|tempco|type",
                       re.I)


def _si(v: float, unit: str) -> str:
    for m, p in ((1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"),
                 (1e-12, "p")):
        if v >= m * 0.999:
            return f"{v / m:g} {p}{unit}"
    return f"{v:g} {unit}"


def judge(kind: str, value: str, footprint: str, props: dict | None = None,
          path: str | None = None):
    """One C or R part against its case. None when not judged; else
    {'ok', 'case', 'stated', 'limit', 'unit', 'cites', 'what'}: `what` says
    in words what was compared ("4.7 uF X7R at 50 V in 0402")."""
    import engcalc
    import mlcc
    case = engcalc.package_code(footprint)
    if not case or kind not in ("C", "R"):
        return None
    texts = [value or ""] + [str(v) for k, v in (props or {}).items()
                             if _PROP_KEY.search(k)]
    blob = " ".join(texts)
    if kind == "R":
        watts = None
        for t in texts:
            watts = engcalc.parse_watts(t)
            if watts:
                break
        lim = res_limit(case, path)
        if not watts or not lim:
            return None
        return {"ok": watts <= R_MARGIN * lim[0], "case": case,
                "stated": watts, "limit": lim[0], "unit": "W",
                "margin": R_MARGIN, "cites": lim[1],
                "what": f"{_si(watts, 'W')} in {case}"}
    c = mlcc.classify(value or "", footprint or "", blob)
    if c["kind"] in ("electrolytic", "tantalum", "film"):
        return None
    diel = None if c["assumed"] else c["dielectric"]
    if diel is not None and diel not in DIELECTRICS:
        return None  # Y5V, X6S, X7S...: no chart rows for them here
    farads = engcalc.parse_value(re.sub(
        r"(\d)\s+(?=[pnuµm])", r"\1", (value or "").replace("μ",
                                                            "µ")))
    if not farads or farads < 1e-13 or farads > 1:
        return None
    volts = None
    for t in texts:
        volts = engcalc.parse_volts(t)
        if volts:
            break
    # a C0G is held to the case's largest part of any dielectric: the
    # makers' class-1 charts differ most (TDK and Murata list 10 nF C0G in
    # 0402, Yageo 2.2 nF)
    lim = cap_limit(case, diel if diel in ("X5R", "X7R") else None, volts,
                    path)
    if not lim:
        return None
    return {"ok": farads <= MARGIN * lim[0], "case": case, "stated": farads,
            "limit": lim[0], "unit": "F", "margin": MARGIN, "cites": lim[1],
            "what": f"{_si(farads, 'F')}"
                    + (f" {diel}" if diel else "")
                    + (f" at {volts:g} V" if volts else "") + f" in {case}"}
