"""mlcc.py - effective capacitance of a ceramic capacitor at DC bias and
temperature, and what kind of capacitor a board part is.

A class-II MLCC (X5R, X7R, X6S, X7S, X8R, Y5V...) loses capacitance as the
field across its dielectric layers grows. The field is V / t, and for a
given package the layer thickness t falls as the capacitance packed into it
rises: C ~ eps * A * N / t with N ~ H / t, so t ~ sqrt(eps * volume / C).
The bias at which half the capacitance is gone is therefore

    v_half = A * (V_rated / 10)^0.25 * sqrt(volume_mm3 / C_uF)   (A in V)

(a higher-rated part of the same density is built with a sturdier
dielectric: the rating term) and the remaining fraction is
1 / (1 + (V / v_half)^K). A and K are fitted to the derated values
hifi-receiver-2's first-spin power review read from makers' curves
(docs/review-2/power.md: 10 uF/10 V 0603 at 5.2 V ~ 50 %, 2 x 22 uF/25 V
0805 at 5.2 V ~ 22 uF, 10 uF/50 V 1206 at 28 V ~ 3-4 uF) plus a 1 uF/10 V
0402 at 3.3 V ~ 70 %. Those points fit A between about 10 and 16 V (K = 2);
A_TYP is the middle and A_LOW the low edge, so `c_eff_low` is the
pessimistic reading a loop or hold-up margin should be checked with. With
no rating known the rating term is 1 (a 10 V part). It is
a typical-curve screen, not a maker's curve: a part listed in
reference/mlcc_dcbias.yaml by MPN uses its own curve points instead, and a
result from the generic curve says so (`curve: generic`).

Temperature: each class-II code's worst-case change at its upper
temperature (X5R/X7R +-15 %, X6S/X7S +-22 %, Y5V +22/-82 %) is applied
linearly from 25 C to that temperature; the two effects are multiplied.
Class I (C0G/NP0), film, electrolytic, polymer and tantalum parts keep their
nominal value here (their own derating - ripple, ESR - is another check).
"""
from __future__ import annotations

import functools
import math
import re
from pathlib import Path

import yaml

A_TYP = 13.0
A_LOW = 10.0
K = 2.0
REF_YAML = Path(__file__).resolve().parents[2] / "reference" / "mlcc_dcbias.yaml"

# L x W x T in mm; T is the thick (high-capacitance) body of the size.
PACKAGE_MM = {"0201": (0.6, 0.3, 0.3), "0402": (1.0, 0.5, 0.5),
              "0603": (1.6, 0.8, 0.8), "0805": (2.0, 1.25, 1.25),
              "1206": (3.2, 1.6, 1.6), "1210": (3.2, 2.5, 2.5),
              "1812": (4.5, 3.2, 2.5), "2220": (5.7, 5.0, 2.5)}

# code: (worst fractional change at its top temperature, top temperature C)
TEMPCO = {"X5R": (0.15, 85), "X6S": (0.22, 105), "X7R": (0.15, 125),
          "X7S": (0.22, 125), "X7T": (0.33, 125), "X8R": (0.15, 150),
          "X8L": (0.40, 150), "Y5V": (0.82, 85), "Z5U": (0.56, 85)}
# Y5V/Z5U lose capacitance at roughly twice the field of X5R/X7R.
STRONG_BIAS = {"Y5V": 0.5, "Z5U": 0.5}

CLASS2_RE = re.compile(r"\b([XYZ]\d[A-Z])\b", re.I)
CLASS1_RE = re.compile(r"\b(C0G|COG|NP0|NPO|U2J|P100|SL)\b", re.I)
ELECTRO_RE = re.compile(r"CP_Elec|Elec|ELEC|Electrolytic|Polymer|POSCAP|"
                        r"OS-?CON|CAP-SMD_BD|CAP-TH_BD|Radial|CP_Radial|"
                        r"Aluminum|Alum|\bAl\b|E-CAP", re.I)
TANT_RE = re.compile(r"Tantal|CP_Tantalum|TAJ|TPS[A-E]|T491|T520|\bTant",
                     re.I)
FILM_RE = re.compile(r"\bFilm\b|C_Rect|MKT|MKP|WIMA", re.I)


def classify(value: str = "", footprint: str = "", text: str = "") -> dict:
    """{'kind': class2|class1|electrolytic|tantalum|film|unknown,
    'dielectric': code or None, 'assumed': bool} for a capacitor from its
    value, footprint and any MPN/description text. A chip-size ceramic
    footprint with no dielectric named is taken as X5R when it is 1 uF or
    more (what a decoupling/bulk ceramic almost always is), class I below
    1 nF, and X7R between - `assumed` says the code was not read."""
    blob = " ".join([value or "", footprint or "", text or ""])
    m = CLASS2_RE.search(blob)
    if m and m.group(1).upper() in TEMPCO:
        return {"kind": "class2", "dielectric": m.group(1).upper(),
                "assumed": False}
    if CLASS1_RE.search(blob):
        return {"kind": "class1", "dielectric": "C0G", "assumed": False}
    if TANT_RE.search(blob):
        return {"kind": "tantalum", "dielectric": None, "assumed": False}
    if ELECTRO_RE.search(blob):
        return {"kind": "electrolytic", "dielectric": None, "assumed": False}
    if FILM_RE.search(blob):
        return {"kind": "film", "dielectric": None, "assumed": False}
    if package(footprint) is None:
        return {"kind": "unknown", "dielectric": None, "assumed": True}
    c = _value_f(value)
    if c is not None and c < 1e-9:
        return {"kind": "class1", "dielectric": "C0G", "assumed": True}
    if c is not None and c >= 1e-6:
        return {"kind": "class2", "dielectric": "X5R", "assumed": True}
    return {"kind": "class2", "dielectric": "X7R", "assumed": True}


def _value_f(text) -> float | None:
    import engcalc
    return engcalc.parse_value(text)


def package(footprint: str | None) -> str | None:
    import engcalc
    code = engcalc.package_code(footprint)
    if code:
        return code
    m = re.search(r"(?<!\d)(1812|2220)(?!\d)", str(footprint or ""))
    return m.group(1) if m else None


def load_table(path: Path = REF_YAML) -> list[dict]:
    if not path.is_file():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "parts") or []


def _table_fraction(row: dict, vdc: float) -> float:
    """Linear interpolation over a row's [volts, fraction] points."""
    pts = sorted((float(v), float(f)) for v, f in row["curve"])
    if vdc <= pts[0][0]:
        return pts[0][1]
    for (v0, f0), (v1, f1) in zip(pts, pts[1:]):
        if vdc <= v1:
            return f0 + (vdc - v0) / (v1 - v0) * (f1 - f0)
    return pts[-1][1]


def bias_fraction(c_f: float, pkg: str | None, vdc: float,
                  dielectric: str = "X5R", a: float = A_TYP,
                  rated_v: float | None = None) -> float | None:
    """Remaining fraction of capacitance at DC bias vdc (generic curve)."""
    if pkg not in PACKAGE_MM or c_f <= 0:
        return None
    l, w, t = PACKAGE_MM[pkg]
    v_half = a * STRONG_BIAS.get(dielectric, 1.0) * math.sqrt(
        l * w * t / (c_f * 1e6)) * ((rated_v or 10.0) / 10.0) ** 0.25
    return 1.0 / (1.0 + (abs(vdc) / v_half) ** K)


def temp_fraction(dielectric: str | None, temp_c: float) -> float:
    tc, top = TEMPCO.get((dielectric or "").upper(), (0.0, 125))
    if temp_c <= 25 or tc == 0:
        return 1.0
    return 1.0 - tc * min(1.0, (temp_c - 25) / (top - 25))


def c_eff(c_f: float, vdc: float, *, footprint: str = "", value: str = "",
          text: str = "", mpn: str = "", rated_v: float | None = None,
          temp_c: float = 25.0, table: list[dict] | None = None) -> dict:
    """Effective capacitance of one capacitor. Returns
    {c_nom, c_eff, c_eff_low, fraction, kind, dielectric, package, curve,
    vdc, temp_c, notes}; c_eff == c_nom for parts that do not derate here."""
    cls = classify(value, footprint, " ".join([text, mpn]))
    pkg = package(footprint)
    out = {"c_nom": c_f, "vdc": vdc, "temp_c": temp_c, "kind": cls["kind"],
           "dielectric": cls["dielectric"], "package": pkg,
           "dielectric_assumed": cls["assumed"], "notes": []}
    if rated_v and abs(vdc) > rated_v:
        out["notes"].append(f"{abs(vdc):.3g} V is over the {rated_v:g} V "
                            "rating")
    if cls["kind"] != "class2":
        out.update(c_eff=c_f, c_eff_low=c_f, fraction=1.0, curve="none")
        return out
    row = None
    for r in table if table is not None else load_table():
        if mpn and re.search(r["match"], mpn, re.I):
            row = r
            break
    ft = temp_fraction(cls["dielectric"], temp_c)
    if row is not None:
        fb = _table_fraction(row, abs(vdc))
        out.update(c_eff=c_f * fb * ft, c_eff_low=c_f * fb * ft,
                   fraction=fb * ft, curve=f"table: {row.get('source', '')}")
        return out
    fb = bias_fraction(c_f, pkg, vdc, cls["dielectric"], A_TYP, rated_v)
    if fb is None:
        out["notes"].append("package size not read from the footprint: "
                            "no DC-bias estimate")
        out.update(c_eff=c_f * ft, c_eff_low=c_f * ft, fraction=ft,
                   curve="unknown package")
        return out
    fl = bias_fraction(c_f, pkg, vdc, cls["dielectric"], A_LOW, rated_v)
    out.update(c_eff=c_f * fb * ft, c_eff_low=c_f * fl * ft,
               fraction=fb * ft, curve="generic")
    return out


# --- rated voltage from the part number or the Value field (#1307) ---------

VCODE_YAML = (Path(__file__).resolve().parents[2] / "reference"
              / "mlcc_voltage_codes.yaml")


def load_vcodes(path: Path = VCODE_YAML) -> list[dict]:
    """The makers' voltage-code entries, each with its regex compiled."""
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [{**e, "_rx": re.compile(e["match"])}
            for e in data.get("series") or []]


@functools.lru_cache(maxsize=1)
def _vcodes() -> tuple[dict, ...]:
    return tuple(load_vcodes())


def mpn_voltage(mpn: str, table: list[dict] | None = None) -> dict | None:
    """{v, code, series, cite} when the MPN matches a maker's ordering code
    in full and its voltage code has a row in the table; else None (a code
    with no row is never guessed)."""
    key = re.sub(r"\s+", "", str(mpn or "")).upper()
    if not key:
        return None
    for e in _vcodes() if table is None else table:
        m = e["_rx"].fullmatch(key)
        if not m:
            continue
        code = m.group("v")
        v = {str(k): x for k, x in (e.get("codes") or {}).items()}.get(code)
        if v is None:
            return None
        return {"v": float(v), "code": code, "series": e["name"],
                "cite": e["cite"]}
    return None


# "16V", "6.3 V", "50VDC", "2kV", "6V3" - with a separator (or nothing) on
# each side, so "_10V_" in "C_0.1u_0402_10V_10%" counts and "V" inside a
# word (VCC, 10VA) does not
_VALUE_V_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:(\d+(?:\.\d+)?)\s*(k?)V(?:DC)?|(\d+)V(\d))"
    r"(?![A-Za-z0-9])")


def value_voltage(value: str) -> dict | None:
    """{v, text} for the one voltage written in a capacitor's Value field
    ("220u/16V", "C_0.1u_0402_10V_10%_JB", "4u7 6V3"); None when there is
    none, or more than one different figure."""
    found = {}
    for m in _VALUE_V_RE.finditer(str(value or "")):
        if m.group(1):
            v = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
        else:
            v = float(f"{m.group(3)}.{m.group(4)}")
        found.setdefault(v, m.group(0))
    if len(found) != 1 or next(iter(found)) <= 0:
        return None
    (v, text), = found.items()
    return {"v": v, "text": text}
