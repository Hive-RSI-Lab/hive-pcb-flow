"""pinmaplib.py - a placed symbol's pin map against the real part (miss
class 5 of docs/miss-taxonomy.md; #1086, #1114).

netlist_audit.py's symbol rules read these helpers:

 - stock symbols: the pin map (number -> name) and default footprint of
   KiCad's own symbol for a part, looked up by the placed part's Value or
   MPN (exact name, a stock name whose lowercase x stands for one
   character, or the same 74-series logic function in another family);
 - pin map compare: names both maps carry, whose pin numbers differ;
 - size tokens: a dimension (0.8in, 12mm) in a symbol name that the
   part's Value or footprint contradicts;
 - sibling channels: two units of one part (1QA..1QD, 2QA..2QD) whose
   nets count in opposite directions;
 - I2C roles: which nets a board means as SDA and as SCL.

Nothing here reads a netlist file; netlist_audit passes its parse in.
"""
from __future__ import annotations

import functools
import os
import re
from pathlib import Path

# ------------------------------------------------------------ pin names

# names that say the same pin; folded before two maps are compared
_PIN_ALIASES = {"VIN": "IN", "VI": "IN", "VOUT": "OUT", "VO": "OUT",
                "VCC": "VCC", "GROUND": "GND", "PGND": "PGND"}
# names that carry no function to compare
_NO_FUNC_RE = re.compile(r"^(NC|N\.C\.|DNC|NU|RFU|~|-|\d+|PIN_?\d+|P\d+)$")


def pin_names(name: str | None) -> set[str]:
    """The names one pin answers to: upper case, overbars dropped, split
    on '/' alternatives (ADJ/GND is ADJ and GND), aliases folded (VIN and
    VI are IN). NC and bare numbers name no function and give nothing."""
    raw = re.sub(r"~\{([^}]*)\}", r"\1", name or "").replace("~", "")
    out = set()
    for part in raw.upper().split("/"):
        part = part.strip()
        if not part or _NO_FUNC_RE.match(part):
            continue
        out.add(_PIN_ALIASES.get(part, part))
    return out


def by_name(pinmap: dict[str, str]) -> dict[str, set[str]]:
    """{pin number: name} -> {folded name: {pin numbers}}."""
    out: dict[str, set[str]] = {}
    for num, name in pinmap.items():
        for n in pin_names(name):
            out.setdefault(n, set()).add(str(num))
    return out


def pinmap_diffs(placed: dict[str, str], stock: dict[str, str]
                 ) -> list[tuple[str, list[str], list[str]]]:
    """[(name, placed pins, stock pins)] for each function name both maps
    carry whose pins disagree: a different single pin, or, for a name on
    several pins (GND on 2 and the tab), no pin in common. Names only one
    side has are not judged: a custom symbol may name pins its own way."""
    a, b = by_name(placed), by_name(stock)
    out = []
    for name in sorted(set(a) & set(b)):
        pa, pb = a[name], b[name]
        if pa == pb:
            continue
        if (len(pa) == 1 and len(pb) == 1) or not pa & pb:
            out.append((name, sorted(pa, key=_num_key),
                        sorted(pb, key=_num_key)))
    return out


def _num_key(s: str):
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


# ------------------------------------------------------------ packages

def package_family(footprint: str | None) -> str | None:
    """A footprint's package token, dashes dropped: SOT-223-3_TabPin2 is
    SOT2233, TO-252-2 is TO2522, SOIC-8_3.9x4.9mm is SOIC8. None when the
    name starts with no letters-then-digits token (a custom 'MyPart')."""
    name = (footprint or "").split(":")[-1].upper().replace("-", "")
    m = re.match(r"^([A-Z]{2,}\d+)", name)
    return m.group(1) if m else None


def same_package(fp_a: str | None, fp_b: str | None) -> bool:
    """True unless both footprints name a package and the two differ
    (an unknown package is given the benefit of the doubt)."""
    a, b = package_family(fp_a), package_family(fp_b)
    if not a or not b:
        return True
    # SOT-223 vs SOT-223-3: one token a prefix of the other is one package
    return a.startswith(b) or b.startswith(a)


# ------------------------------------------------------------ stock symbols

# stock libraries whose symbols are generic (R, LED, Conn_01x04): a Value
# that names one of them names no particular part
_GENERIC_LIB_RE = re.compile(
    r"^(Device|power|Connector.*|Mechanical|Jumper|Switch|Graphic|pspice|"
    r"Simulation_SPICE|Logo|Motor|Transformer)$")
_LOGIC_RE = re.compile(
    r"^(?:SN|MC|CD|HD|M|NLV|NL|IDT|MM)?74([A-Z]{0,5})(\d{2,5})"
    r"([A-Z]{0,4}\d{0,2})$")


def logic_key(name: str) -> str | None:
    """'74:393' for any family of a 74-series function (SN74LV393A,
    74HC393D, 74LS393), else None. Single-gate parts (74LVC1G08) have
    their own pinouts and get no key."""
    m = _LOGIC_RE.match((name or "").upper())
    if not m or "G" in m.group(1)[-1:]:
        return None
    return f"74:{m.group(2)}"


@functools.lru_cache(maxsize=4)
def stock_index(symdir: str) -> dict:
    """Index of a KiCad stock symbol directory (KiCad 10 *.kicad_symdir
    folders of one-symbol files): {"exact": {NAME: [(lib, name, path)]},
    "logic": {key: [...]}, "wild": [(regex, (lib, name, path))]}. Only the
    file names are read here; a symbol's pins are read when first asked."""
    exact: dict[str, list] = {}
    logic: dict[str, list] = {}
    wild: list = []
    root = Path(symdir)
    try:
        libs = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        libs = []
    for lib in libs:
        if not lib.name.endswith(".kicad_symdir") or not lib.is_dir():
            continue
        lname = lib.name[:-len(".kicad_symdir")]
        if _GENERIC_LIB_RE.match(lname):
            continue
        for f in os.scandir(lib.path):
            if not f.name.endswith(".kicad_sym"):
                continue
            sname = f.name[:-len(".kicad_sym")]
            hit = (lname, sname, f.path)
            exact.setdefault(sname.upper(), []).append(hit)
            k = logic_key(sname)
            if k:
                logic.setdefault(k, []).append(hit)
            if "x" in sname and len(sname.replace("x", "")) >= 4:
                pat = "".join("[A-Z0-9]" if c == "x" else re.escape(c.upper())
                              for c in sname)
                wild.append((re.compile(f"^{pat}$"), hit))
    return {"exact": exact, "logic": logic, "wild": wild}


def stock_candidates(names: list[str], symdir: str | Path | None
                     ) -> list[tuple[str, str, str]]:
    """[(lib, symbol, path)] of stock symbols the part names (its Value,
    MPN) call for: exact names first, then x-wildcard names, then the same
    74-series function in another family. Empty with no stock dir."""
    if not symdir:
        return []
    idx = stock_index(str(symdir))
    for name in names:
        n = (name or "").strip().upper()
        if not n:
            continue
        if n in idx["exact"]:
            return list(idx["exact"][n])
        wild = [h for rx, h in idx["wild"] if rx.match(n)]
        if wild:
            return wild
        k = logic_key(n)
        if k and k in idx["logic"]:
            return list(idx["logic"][k])
    return []


def _sexp_symbols(path: str) -> list:
    import sexpdata  # noqa: PLC0415
    with open(path, encoding="utf-8") as fh:
        data = sexpdata.load(fh)
    return [x for x in data if isinstance(x, list) and x
            and str(x[0]) == "symbol"]


def _walk_pins(node, out: dict[str, str]):
    for x in node:
        if not isinstance(x, list) or not x:
            continue
        head = str(x[0])
        if head == "pin":
            name = num = None
            for y in x:
                if isinstance(y, list) and len(y) > 1:
                    if str(y[0]) == "name":
                        name = str(y[1])
                    elif str(y[0]) == "number":
                        num = str(y[1])
            if num is not None:
                out.setdefault(num, name or "")
        elif head == "symbol":
            _walk_pins(x, out)


@functools.lru_cache(maxsize=512)
def stock_symbol(path: str) -> dict | None:
    """{"pins": {number: name}, "footprint": str} of the one-symbol file
    PATH, following (extends "Parent") to the parent's file beside it.
    None when it can't be read."""
    try:
        syms = _sexp_symbols(path)
    except Exception:  # noqa: BLE001 - a stock file we can't read is no hit
        return None
    if not syms:
        return None
    sym = syms[0]
    fp = ""
    parent = None
    for x in sym:
        if isinstance(x, list) and len(x) > 2 and str(x[0]) == "property" \
                and str(x[1]) == "Footprint":
            fp = str(x[2])
        elif isinstance(x, list) and len(x) > 1 and str(x[0]) == "extends":
            parent = str(x[1])
    pins: dict[str, str] = {}
    _walk_pins(sym, pins)
    if parent and not pins:
        base = stock_symbol(str(Path(path).with_name(f"{parent}.kicad_sym")))
        if base is None:
            return None
        pins = dict(base["pins"])
        fp = fp or base["footprint"]
    return {"pins": pins, "footprint": fp}


# ------------------------------------------------------------ size tokens

_DIM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(in(?:ch)?|mm|\")"
                     r"(?![a-z])", re.I)


def dimensions(text: str | None) -> set[tuple[float, str]]:
    """{(number, unit)} for each size written in TEXT: 0.8in, 0.56", 12mm.
    Inches are 'in' however written."""
    out = set()
    for num, unit in _DIM_RE.findall(text or ""):
        u = "mm" if unit.lower() == "mm" else "in"
        out.add((round(float(num), 4), u))
    return out


def size_conflict(symbol: str, value: str | None, footprint: str | None
                  ) -> tuple[set, set, str] | None:
    """(symbol sizes, other sizes, 'Value'|'footprint') when the symbol's
    name carries a size its part's Value (or, if the Value has none in
    that unit, its footprint name) contradicts with no size in common:
    a '7Seg 0.2in' symbol on a 'SMD 0.8in' digit. Else None."""
    mine = dimensions(symbol)
    if not mine:
        return None
    for where, text in (("Value", value),
                        ("footprint", (footprint or "").split(":")[-1])):
        theirs = dimensions(text)
        units = {u for _, u in mine} & {u for _, u in theirs}
        if not units:
            continue
        a = {d for d in mine if d[1] in units}
        b = {d for d in theirs if d[1] in units}
        return (a, b, where) if not a & b else None
    return None


def fmt_dims(dims: set) -> str:
    return ", ".join(f"{n:g}{u}" for n, u in sorted(dims))


# ------------------------------------------------------------ sibling channels

_CHANNEL_RE = re.compile(r"^(\d)([A-Z]{1,3}?)([A-H]|\d{1,2})$")


def _channel(name: str | None) -> tuple[str, str, str] | None:
    """('1', 'Q', 'A') for pin 1QA: unit digit, stem, index."""
    n = re.sub(r"~\{([^}]*)\}", r"\1", name or "").replace("~", "").upper()
    m = _CHANNEL_RE.match(n)
    return m.groups() if m else None


def _idx_key(idx: str) -> int:
    return int(idx) if idx.isdigit() else ord(idx) - ord("A")


def _counting(nets: list[str]) -> list[int] | None:
    """The one number that varies along NETS (CLK_50d2MHZ, CLK_50d4MHZ:
    2, 4) when every other part of their names agrees, else None."""
    parts = [re.split(r"(\d+)", n) for n in nets]
    if len({len(p) for p in parts}) != 1:
        return None
    varying = [i for i in range(1, len(parts[0]), 2)
               if len({p[i] for p in parts}) > 1]
    fixed = [i for i in range(0, len(parts[0])) if i not in varying]
    if len(varying) != 1 or any(len({p[i] for p in parts}) > 1
                                for i in fixed):
        return None
    return [int(p[varying[0]]) for p in parts]


def _direction(vals: list[int]) -> int:
    if all(a < b for a, b in zip(vals, vals[1:])):
        return 1
    if all(a > b for a, b in zip(vals, vals[1:])):
        return -1
    return 0


def flipped_channels(pins: dict[str, tuple[str, str]]
                     ) -> list[dict]:
    """Sibling channels of one part that count opposite ways: pins
    {number: (name, net)}; units with the same stem (1QA..1QD and
    2QA..2QD), three or more pins each, whose nets carry one varying
    number that rises along one unit's index and falls along another's.
    A symbol drawn with one unit's pins in reverse order wires exactly
    this (errata corpus: gcm v5.0's SN74LV393A, #1086)."""
    groups: dict[str, dict[str, list]] = {}
    for num, (name, net) in pins.items():
        ch = _channel(name)
        if not ch or not net or net.startswith("unconnected-"):
            continue
        unit, stem, idx = ch
        groups.setdefault(stem, {}).setdefault(unit, []).append(
            (_idx_key(idx), name, num, net))
    out = []
    for stem, units in sorted(groups.items()):
        dirs = {}
        for unit, rows in units.items():
            if len(rows) < 3 or len({r[0] for r in rows}) != len(rows):
                continue
            rows.sort()
            vals = _counting([r[3].split("/")[-1] for r in rows])
            d = _direction(vals) if vals else 0
            if d:
                dirs[unit] = (d, rows, vals)
        if {1, -1} <= {d for d, _, _ in dirs.values()}:
            out.append({"stem": stem, "units": {
                u: {"direction": "up" if d == 1 else "down",
                    "pins": [(r[1], r[2], r[3]) for r in rows],
                    "values": vals}
                for u, (d, rows, vals) in sorted(dirs.items())}})
    return out


# ------------------------------------------------------------ I2C roles

def _tail_tokens(net: str) -> tuple[str, str] | None:
    """(stem, last token) of a net name: OLED_SCK is ('OLED', 'SCK')."""
    tail = re.split(r"[/.]", net.upper())[-1]
    parts = re.split(r"[_\-]", tail)
    if len(parts) < 2:
        return ("", tail)
    return ("_".join(parts[:-1]), parts[-1])


def i2c_net_roles(net_names) -> dict[str, str]:
    """{net: 'SDA'|'SCL'} for nets plainly named as an I2C line: a last
    token SDA or SCL, or SCK / CLK when a net of the same stem is SDA
    (an OLED module's SCK is its I2C clock)."""
    roles: dict[str, str] = {}
    sda_stems = set()
    toks = {}
    for net in net_names:
        if net.startswith("unconnected-"):
            continue
        t = _tail_tokens(net)
        if not t:
            continue
        toks[net] = t
        if t[1] == "SDA":
            sda_stems.add(t[0])
    for net, (stem, last) in toks.items():
        if last in ("SDA", "SCL"):
            roles[net] = last
        elif last in ("SCK", "CLK") and stem in sda_stems:
            roles[net] = "SCL"
    return roles


def mcu_i2c_function(pin_name: str, table: dict[str, str]) -> str | None:
    """The I2C function ('I2C0_SDA') an MCU row's i2c table gives a pin:
    by its whole first name, else its leading letters-and-digits
    (GPIO26_ADC0 is GPIO26, GP4 is GPIO4)."""
    base = re.split(r"[-/]", (pin_name or "").upper())[0]
    if base in table:
        return table[base]
    m = re.match(r"^([A-Z]+)(\d+)", base)
    if not m:
        return None
    if m.group(1) == "GP":  # Pico module symbols say GP4 for GPIO4
        return table.get(f"GPIO{m.group(2)}")
    return table.get(m.group(1) + m.group(2))
