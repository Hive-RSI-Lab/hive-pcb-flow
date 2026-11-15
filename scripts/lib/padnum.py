"""padnum.py - which pad carries which pin (fp_verify's numbering checks).

A footprint can match the land pattern pad for pad and still put the pins on
the wrong pads: numbered from the datasheet's bottom view (a mirror image),
or round from the wrong corner. fp_verify's geometry checks never read a pad
number; these do. Coordinates are KiCad's: millimetres, x right, y DOWN, the
footprint seen from the top. A record pin may meet a pad at its hole, at its
shape (moved by a drill offset) or at its copper box (a custom pad's
primitives: a ring drawn round a port off its anchor), and touching pads that
share a number count as one land too (a split exposed pad); see _pad_views and
_fp_lands.

  fit_record(pads, land)     the part record lists where each pin sits in
      the top view (land_pattern.pads). Fit the record onto the footprint
      (quarter turns, the angle the pin numbers point to and a shift;
      mirrored too, to name a bottom-view cause) and compare the numbers
      spot by spot.
  perimeter_direction(pads)  no per-pin data: on a perimeter package (SOIC,
      SOT, DFN, QFN, LGA ...) top-view numbers run counter-clockwise from
      pin 1; clockwise means the footprint was numbered from a bottom view.
  bga_handedness(pads)       on a BGA seen from the top with A1 at the top
      left, column numbers run right and row letters run down; a footprint
      numbered from the bottom view has one of the two flipped.
  classify(fp_name, *record_names)  which of the two applies, if any:
      connectors, PLCCs, SOT-227 modules and names that declare their
      numbering (clockwise, reversed) are left alone.
  judge(pads, family)        run that family's check (fp_verify leaves
      through-hole footprints out before it asks).

Pure python, no shapely.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# ------------------------------------------------------------ package family

# Perimeter packages: pins round the edge, numbered counter-clockwise from
# pin 1 in the top view (JEDEC). A token may carry a vendor or thickness
# prefix from a short list (TSSOP, HVQFN, X2SON, UTDFN, LQFP ...) and a pin
# count (SOT23, DFN8); the prefix list keeps names like DEGSON out. LFCSP is
# Analog Devices' lead-frame QFN, not a ball grid. The second line is other
# names for the same packages, spelt out whole: SOJ, VSO, SSO (an
# optocoupler's stretched SO), TI's S-/R-PDSO, Infineon's PG-DSO, TSDSO,
# HDSOP and (TS)DSON, ON's Micro8, ST's PowerSSO, UFQFPN and UFDFPN, NXP's
# DHVQFN, ceramic CDFN, Maxim's FC2QFN, Vishay's PowerPAK, Diodes'
# PowerDI3333, Fairchild's SuperSOT and MicroPak, ROHM's VSOF. (pkgname.py
# has its own list of package names, for the BOM checks.) A WLP is a
# wafer-level ball grid.
_PERIM_TOKEN = re.compile(
    r"^(?:SOJ?\d*|(?:X2|[EHLMPQSTUVWX]{1,3})?"
    r"(?:SOIC|SOP|SOT|DFN|QFN|SON|QFP|LGA|MLF|MLP[DQ]?|LLP|LFCSP)\d*|"
    r"[SV]SO\d*|(?:P|TS)?DSO\d*|HDSOP\d*|T?S?DSON\d*|MICRO8|POWERSSO\d*|"
    r"[UW]F[DQ]FPN\d*|DH[VW]QFN\d*|CDFN\d*|FC2QFN\d*|POWERPAK\d*|"
    r"POWERDI\d{4}|SUPERSOT\d*|MICROPAK\d*|H?VSOF\d*)$")
_PERIM_NAME = re.compile(r"(?<![A-Z0-9])SC-?(?:59|70|74|75|82|88|89)(?![0-9])")
_BGA_TOKEN = re.compile(r"^(?!LFCSP)[A-Z]{0,4}(?:BGA|CSP|WLP)\d*$")
# Perimeter-shaped but not numbered that way: a PLCC LED (WS2812B and kin)
# numbers its four pads clockwise, and its datasheet agrees, so every PLCC is
# left alone (a PLCC chip carrier, numbered counter-clockwise from the middle
# of a side, could be judged, but isn't yet); SOT-227 (ISOTOP) is a
# screw-terminal module.
_EXCLUDE_TOKEN = re.compile(r"^(?:PLCC\d*|ISOTOP)$")
_EXCLUDE_NAME = re.compile(r"(?<![A-Z0-9])SOT-?227")
# A name that says its numbering is clockwise, reversed or swapped (KiCad's
# Bosch_LGA-8_..._ClockwisePinNumbering, ROHM SOT-457_ClockwisePinNumbering,
# SOT-23-3_..._BR-CW, TSOP-I-32_..._Reverse, SOT-143R; a designer's
# SOT-23-3-pinswapped-mosfet, renumbered to fit a G-D-S symbol) is believed,
# not judged. "Counter-clockwise" and a CCW token are the normal way round.
_DECLARED = re.compile(
    r"(?<!COUNTER)(?<!COUNTER[-_ ])(?<!ANTI)(?<!ANTI[-_ ])CLOCKWISE|REVERSE|"
    r"(?<![A-Z0-9])CW(?![A-Z0-9])|(?<![A-Z0-9])SOT-?\d+R(?![A-Z0-9])|"
    r"PIN[-_ ]?SWAP|SWAPPED")
# Connectors number by their own scheme (odd/even rows, A/B sides). `_FIRST`
# only as the name's first token (TE_, SD_ ...), `_ANY` anywhere.
_CONNECTOR_FIRST = re.compile(r"^(?:TE|SD|SIM|JACK|HEADER|SOCKET)$")
_CONNECTOR_ANY = re.compile(
    r"^(?:CONN|CONNECTOR|PINHEADER|PINSOCKET|PINHEADERS|TERMINAL|"
    r"TERMINALBLOCK|JST|MOLEX|HIROSE|AMPHENOL|SAMTEC|WURTH|DEGSON|PHOENIX|"
    r"USB|USBC|MICROUSB|MINIUSB|HDMI|RJ\d+|MICROSD|FPC|FFC|BARREL|"
    r"RECEPTACLE|PLUG)$")


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t]


def classify(fp_name: str | None, *record_names: str | None) -> tuple[str | None, str]:
    """Which direction check applies, from the footprint's name and the
    record's package strings (land_pattern.package, package):
    ('perimeter' | 'bga', the token that decided it), or (None, why) with
    why 'connector', 'excluded' (a PLCC, a SOT-227 module), 'unknown' (no
    package family in any name) or 'declared' (a name says the numbering is
    clockwise, reversed or swapped).

    Connector words count in the footprint's name only: a record's prose
    ("8-terminal LGA") says TERMINAL about an LGA's lands. The rest count in
    every name. A 'Lib:' prefix comes off the footprint's name."""
    fp_u = (fp_name or "").upper().split(":")[-1]
    names_u = [n for n in [fp_u] + [r.upper() for r in record_names if r] if n]
    fp_toks = _tokens(fp_u)
    if fp_toks and _CONNECTOR_FIRST.match(fp_toks[0]) or \
            any(_CONNECTOR_ANY.match(t) for t in fp_toks):
        return None, "connector"
    toks = [t for n in names_u for t in _tokens(n)]
    if any(_EXCLUDE_NAME.search(n) for n in names_u) or \
            any(_EXCLUDE_TOKEN.match(t) for t in toks):
        return None, "excluded"
    family = token = None
    for t in toks:
        if _BGA_TOKEN.match(t):
            family, token = "bga", t
            break
    if family is None:
        for t in toks:
            if _PERIM_TOKEN.match(t):
                family, token = "perimeter", t
                break
    if family is None:
        for n in names_u:
            m = _PERIM_NAME.search(n)
            if m:
                family, token = "perimeter", m.group(0)
                break
    if family is None:
        return None, "unknown"
    if any(_DECLARED.search(n) for n in names_u):
        return None, "declared"
    return family, token


def judge(pads, family: str) -> dict | None:
    """The family's direction check on `pads` (fplib pads, or RecPads for a
    record's own pins): {'family': 'perimeter', **perimeter_direction} or
    {'family': 'bga', **bga_handedness}; None with too few usable pads. A
    perimeter package whose pads are mostly named by grid (A1, B7 ...: an
    LGA module) is judged like a BGA."""
    pads = list(pads)
    gridded = sum(bool(_BALL.match(p.number.strip().upper())) for p in pads)
    if family == "perimeter" and pads and gridded >= 0.75 * len(pads):
        family = "bga"
    res = perimeter_direction(pads) if family == "perimeter" \
        else bga_handedness(pads)
    return None if res is None else {"family": family, **res}


# ------------------------------------------------------------ geometry helpers

# A pad with copper over this many times the median pad's is a tab or an
# exposed pad, not a pin on the outline.
_BIG_PAD = 2.0


def _median_area(areas: list[float]) -> float:
    """The lower median of `areas` (0 when empty)."""
    areas = sorted(areas)
    return areas[(len(areas) - 1) // 2] if areas else 0.0


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull, counter-clockwise ON SCREEN (y down), collinear points
    dropped. Computed in y-up coordinates, where screen-CCW is the usual
    positive turn, and returned in y-down."""
    up = sorted({(x, -y) for x, y in pts})
    if len(up) < 3:
        return [(x, -y) for x, y in up]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in up:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-12:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(up):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-12:
            upper.pop()
        upper.append(p)
    return [(x, -y) for x, y in lower[:-1] + upper[:-1]]


def _perimeter_position(p, hull) -> tuple[float, float]:
    """(distance from p to the hull's boundary, arc length along the boundary
    to p's nearest point), the arc measured counter-clockwise on screen from
    the hull's first vertex."""
    best = (math.inf, 0.0)
    run = 0.0
    for i, a in enumerate(hull):
        b = hull[(i + 1) % len(hull)]
        ex, ey = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(ex, ey)
        t = 0.0 if ln == 0 else max(0.0, min(1.0, (
            (p[0] - a[0]) * ex + (p[1] - a[1]) * ey) / (ln * ln)))
        d = math.hypot(a[0] + t * ex - p[0], a[1] + t * ey - p[1])
        if d < best[0] - 1e-9:
            best = (d, run + t * ln)
        run += ln
    return best


# ------------------------------------------------------- perimeter direction

def perimeter_direction(pads) -> dict | None:
    """Which way the numbers run round a perimeter package, seen from the top.

    Takes the pads with plain numbers, drops tabs and exposed pads (copper
    over _BIG_PAD times the median, which counts each number once, at its
    largest pad) with the drilled pads that share their number (thermal
    vias, which KiCad numbers like the exposed pad and which can stand past
    the pins' centres, out on the outline) and interior ones (a centre row:
    centre farther from the outline than half the pad's smaller side), and
    walks the rest counter-clockwise round the outline (`order`). Pads in a
    row with one number count once (a lead drawn in pieces). Top-view
    numbering rises all the way round from the lowest number and falls
    once, at the wrap (gaps allowed: a pin left out); a footprint numbered
    from the bottom view falls all the way round. The three lowest numbers
    must also turn the same way about the ring's centroid (`winding`, the
    turn of pins 1 -> 2 -> 3). `direction` is 'ccw' or 'cw' when both
    agree, else None: an odd scheme, such as a staggered row (1, 3, 5, 4,
    2), a number on two sides of the outline, or names that are not pin
    numbers (a land named 13, 4 and 58: a ring whose top number is over
    twice its count of numbers). `up` and `down` count the steps of exactly
    +1 and -1. None when fewer than three numbered pads sit on the outline
    or they lie on one line."""
    pts = []
    for p in pads:
        num = p.number.strip()
        if not num.isdigit():
            continue
        x, y = p.copper_center
        w, h = p.copper_size
        pts.append((int(num), x, y, min(w, h), w * h,
                    getattr(p, "drill", None) is not None))
    # a tab or an exposed pad is not a pin on the outline: SOT-89's pin 2
    # runs into the tab, and the tab's centre sits inside the body. The
    # median takes each number once, at its largest pad, so nine vias in an
    # SO-8's exposed pad don't make its pins look big; a drilled pad with a
    # tab's number is a thermal via (an undrilled one, SOT-89's pin 2 or a
    # PowerPAK's drain leads, is a pin and stays)
    largest: dict[int, float] = {}
    for q in pts:
        largest[q[0]] = max(largest.get(q[0], 0.0), q[4])
    median = _median_area(list(largest.values()))
    big = {n for n, a in largest.items() if a > _BIG_PAD * median}
    pts = [q[:4] for q in pts
           if q[4] <= _BIG_PAD * median and not (q[5] and q[0] in big)]
    # stacked pads (one land drawn as several) count once, lowest number
    pts.sort()
    uniq: list = []
    for q in pts:
        if not any(abs(q[1] - u[1]) < 1e-3 and abs(q[2] - u[2]) < 1e-3 for u in uniq):
            uniq.append(q)
    if len(uniq) < 3:
        return None
    hull = _hull([(x, y) for _, x, y, _ in uniq])
    if len(hull) < 3:
        return None
    ring = []
    for n, x, y, small in uniq:
        d, s = _perimeter_position((x, y), hull)
        if d <= max(0.5 * small, 0.05):
            ring.append((s, n, x, y))
    if len(ring) < 3:
        return None
    ring.sort()
    order = [n for _, n, _, _ in ring]
    up = down = 0
    for i, n in enumerate(order):
        step = order[(i + 1) % len(order)] - n
        up += step == 1
        down += step == -1
    # one number on neighbouring pads counts once, round the wrap too
    seq = [q for i, q in enumerate(ring) if q[1] != ring[i - 1][1]] or ring[:1]
    nums = [q[1] for q in seq]
    way = None
    if len(nums) >= 3 and len(set(nums)) == len(nums):
        rises = sum(nums[(i + 1) % len(nums)] > n for i, n in enumerate(nums))
        way = "ccw" if rises == len(nums) - 1 else "cw" if rises == 1 else None
    cx = sum(q[2] for q in ring) / len(ring)
    cy = sum(q[3] for q in ring) / len(ring)
    low = sorted(seq, key=lambda q: q[1])[:3]
    winding = _winding([(q[2], q[3]) for q in low], (cx, cy)) \
        if len(low) == 3 else None
    if max(order) > 2 * len(set(order)):
        way = None          # names, not pin numbers
    return {"order": order, "up": up, "down": down, "winding": winding,
            "direction": way if way == winding else None}


def _winding(pts: list[tuple[float, float]], centre: tuple[float, float]) -> str | None:
    """'ccw' or 'cw' on screen (y down): which way the walk pts[0] -> pts[1]
    -> ... turns about `centre`, summing each step's signed angle; None when
    it doesn't turn."""
    total = 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        # y up, where counter-clockwise on screen is the positive turn
        ax, ay, bx, by = ax - centre[0], centre[1] - ay, bx - centre[0], centre[1] - by
        total += math.atan2(ax * by - ay * bx, ax * bx + ay * by)
    return "ccw" if total > 1e-9 else "cw" if total < -1e-9 else None


# ------------------------------------------------------------ BGA handedness

_BALL = re.compile(r"^([A-Z]{1,3})(\d{1,3})$")


def bga_handedness(pads) -> dict | None:
    """Sign of the turn from the column direction (numbers) to the row
    direction (letters), y down: +1 when the rows run a quarter turn
    clockwise from the columns, as in a top view with A1 at the top left
    (numbers right, letters down) however the footprint is turned; -1 when
    one of them is flipped, as in a footprint numbered from the bottom view.
    None with fewer than two rows or two columns of letter-number balls, or
    when they don't form a grid (a row's or a column's balls off one line by
    more than 0.3 pitch: the A and B rings of a multi-row QFN)."""
    balls = []
    for p in pads:
        m = _BALL.match(p.number.strip().upper())
        if m:
            x, y = p.copper_center
            balls.append((m.group(1), int(m.group(2)), x, y))
    rows = sorted({b[0] for b in balls}, key=lambda r: (len(r), r))
    if len(balls) < 4 or len(rows) < 2 or len({b[1] for b in balls}) < 2:
        return None
    if not _is_grid(balls):
        return None
    rank = {r: i for i, r in enumerate(rows)}

    def direction(key):
        vals = [key(b) for b in balls]
        mv = sum(vals) / len(vals)
        mx = sum(b[2] for b in balls) / len(balls)
        my = sum(b[3] for b in balls) / len(balls)
        var = sum((v - mv) ** 2 for v in vals)
        if var == 0:
            return None
        return (sum((b[2] - mx) * (v - mv) for b, v in zip(balls, vals)) / var,
                sum((b[3] - my) * (v - mv) for b, v in zip(balls, vals)) / var)

    col = direction(lambda b: b[1])
    row = direction(lambda b: rank[b[0]])
    if col is None or row is None:
        return None
    norm = math.hypot(*col) * math.hypot(*row)
    if norm == 0:
        return None
    turn = (col[0] * row[1] - col[1] * row[0]) / norm
    return {"turn": round(turn, 3), "col_dir": col, "row_dir": row,
            "mirrored": turn < -0.5}


def _is_grid(balls) -> bool:
    """The balls' rows (same letters) all lie on lines, or their columns
    (same number) do, within 0.3 x the median step between neighbours in a
    row. Staggered and mixed-pitch layouts pass on one of the two; the rings
    of a multi-row QFN (A1-A52 round the outside, B1-B48 inside) fail both
    ways. An array with no line of three balls either way (2 x 2, a
    staggered 2-1-2) can't be tested and passes on its names."""
    def groups(i):
        g: dict = {}
        for b in balls:
            g.setdefault(b[i], []).append(b)
        return list(g.values())
    steps = []
    for row in groups(0):
        row = sorted(row, key=lambda b: b[1])
        steps += [math.hypot(a[2] - b[2], a[3] - b[3]) for a, b in zip(row, row[1:])]
    steps = sorted(d for d in steps if d > 1e-6)
    if not steps:
        return False
    tol = 0.3 * steps[len(steps) // 2]

    def on_line(g) -> bool:
        mx = sum(b[2] for b in g) / len(g)
        my = sum(b[3] for b in g) / len(g)
        sxx = sum((b[2] - mx) ** 2 for b in g)
        syy = sum((b[3] - my) ** 2 for b in g)
        sxy = sum((b[2] - mx) * (b[3] - my) for b in g)
        th = 0.5 * math.atan2(2 * sxy, sxx - syy)
        nx, ny = -math.sin(th), math.cos(th)
        return max(abs((b[2] - mx) * nx + (b[3] - my) * ny) for b in g) <= tol

    tested = False
    for i in (0, 1):
        lines = [g for g in groups(i) if len(g) >= 3]
        if lines and all(on_line(g) for g in lines):
            return True
        tested = tested or bool(lines)
    return not tested


# --------------------------------------------------------------- record fit

@dataclass(frozen=True)
class RecPad:
    """A record pin shaped like an fplib pad (number, copper_center,
    copper_size), so judge() can tell which way the record's own pins run."""
    number: str
    copper_center: tuple[float, float]
    copper_size: tuple[float, float]


def record_as_pads(rec: list[dict]) -> list[RecPad]:
    """record_pads() output as RecPads. Sizes go in only when every pin has
    one, else every pin gets (0, 0): the tab and exposed-pad filter compares
    areas, and needs all of them or none."""
    sized = all(r["w"] is not None and r["h"] is not None for r in rec)
    return [RecPad(r["number"], (r["x"], r["y"]),
                   (r["w"], r["h"]) if sized else (0.0, 0.0)) for r in rec]


def _turn(x: float, y: float, k: int, mirror: bool) -> tuple[float, float]:
    """Mirror (x -> -x: the bottom view's flip) and then turn k quarter turns
    counter-clockwise on screen (KiCad's positive rotation, y down)."""
    if mirror:
        x = -x
    for _ in range(k % 4):
        x, y = y, -x
    return x, y


def _rotate(x: float, y: float, deg: float, mirror: bool) -> tuple[float, float]:
    """_turn by any angle: mirror, then turn `deg` degrees counter-clockwise
    on screen. A quarter turn goes through _turn, exactly."""
    q = deg / 90.0
    if abs(q - round(q)) <= 1e-9:
        return _turn(x, y, int(round(q)), mirror)
    if mirror:
        x = -x
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _pair(v, minimum: float | None = None) -> tuple[float, float] | None:
    """Two finite numbers (not strings, not booleans), each at least
    `minimum` when given; else None."""
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return None
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or \
                not math.isfinite(x) or (minimum is not None and x < minimum):
            return None
        out.append(float(x))
    return out[0], out[1]


def record_pads(land, skipped: list | None = None) -> list[dict]:
    """The record's per-pin top-view positions (land_pattern.pads), cleaned:
    [{number, x, y, w, h}] with w/h None when the record gives no size.

    A pin is left out when it is not an object, has no number (a string or
    an integer, not blank) or no at_mm of two finite numbers; a size_mm that
    is not two finite numbers >= 0 is dropped and the pin kept. Each of these
    goes into `skipped` as {index, number, why}; `pads` that is not a list
    gives [] and one entry with index None."""
    if not isinstance(land, dict) or land.get("pads") is None:
        return []

    def skip(i, number, why):
        if skipped is not None:
            skipped.append({"index": i, "number": number, "why": why})

    items = land["pads"]
    if not isinstance(items, list):
        skip(None, None, "land_pattern.pads is not a list")
        return []
    out = []
    for i, r in enumerate(items):
        if not isinstance(r, dict):
            skip(i, None, "not an object")
            continue
        num = r.get("number")
        if isinstance(num, bool) or not isinstance(num, (str, int)) or \
                not str(num).strip():
            skip(i, None, "no number (a string such as '1', 'A1', 'EP')")
            continue
        num = str(num).strip()
        at = _pair(r.get("at_mm"))
        if at is None:
            skip(i, num, "at_mm is not two finite numbers")
            continue
        size = r.get("size_mm")
        wh = None if size is None else _pair(size, minimum=0.0)
        if size is not None and wh is None:
            skip(i, num, "size_mm is not two finite numbers >= 0: size ignored")
        out.append({"number": num, "x": at[0], "y": at[1],
                    "w": wh[0] if wh else None, "h": wh[1] if wh else None})
    return out


def _min_spacing(rec: list[dict]) -> float:
    best = math.inf
    for i in range(len(rec)):
        for j in range(i + 1, len(rec)):
            d = math.hypot(rec[i]["x"] - rec[j]["x"], rec[i]["y"] - rec[j]["y"])
            if d > 1e-6:
                best = min(best, d)
    return best if best < math.inf else 1.0


def _shares_axis(pts: list[tuple[float, float]], tol: float = 0.01) -> bool:
    """Whether two of the points share a row or a column (within tol mm)."""
    for axis in (0, 1):
        v = sorted(q[axis] for q in pts)
        if any(b - a <= tol for a, b in zip(v, v[1:])):
            return True
    return False


def _off_axis(pads, rec: list[dict]) -> bool:
    """The record looks turned by other than a quarter turn: the footprint's
    pads share rows or columns and the record's pins none. fit_record tries
    other turns only where the pin numbers point, so after no pass such a
    record's fit means nothing either way."""
    return len(rec) >= 2 and \
        _shares_axis([p.copper_center for p in pads]) and \
        not _shares_axis([(r["x"], r["y"]) for r in rec])


# Same-numbered pads whose copper boxes come this close on both axes are one
# land (a split exposed pad, a tab drawn in pieces).
_TOUCH_MM = 0.05
# Views this much farther from a record pin than the nearest one are "here"
# too: stacked pads, a land's box and its pieces.
_HERE_MM = 0.02


def _pad_views(p) -> list[tuple[float, float, float, float]]:
    """Where a footprint pad can meet a record pin, as (x, y, w, h): its hole
    (`at`) and its shape (moved by a drill offset), both with the shape's
    size, and its copper box (a custom pad's primitives). A record gives a
    through-hole pin as the lead or as the land, and a pad drawn off its
    hole (an offset oval, a ring round a port) puts those apart. RecPads
    (tests) have only the copper box."""
    cc, cs = tuple(p.copper_center), tuple(p.copper_size)
    asz = tuple(getattr(p, "anchor_size", cs))
    ac = tuple(getattr(p, "anchor_center", cc))
    c = tuple(getattr(p, "center", ac))
    out: list = []
    for (x, y), (w, h) in ((c, asz), (ac, asz), (cc, cs)):
        v = (float(x), float(y), float(w), float(h))
        if not any(all(abs(a - b) <= 1e-6 for a, b in zip(v, u)) for u in out):
            out.append(v)
    return out


def _box_gap(a, b) -> float:
    """How far apart two boxes (x0, y0, x1, y1) are on the farther axis;
    negative when they overlap on both."""
    return max(max(a[0], b[0]) - min(a[2], b[2]),
               max(a[1], b[1]) - min(a[3], b[3]))


def _fp_lands(pads, rec_count) -> tuple[list[dict], float]:
    """The footprint's lands, [{number, views, area}], and the area that
    makes a land big (a tab, an exposed pad): _BIG_PAD times the median
    pad's.

    A land is one pad, or pads that touch (copper boxes within _TOUCH_MM on
    both axes) and share a number the record lists at most once ('' and
    numbers the record lacks too): a split exposed pad, a tab drawn in
    pieces. A record gives such a land once, at its middle, so the land's
    bounding box is one more view beside its pads'. A number the record
    lists more than once keeps its pads apart: each may be a spot of its
    own."""
    items = []
    for p in pads:
        (cx, cy), (w, h) = p.copper_center, p.copper_size
        items.append((p.number.strip(),
                      (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                      _pad_views(p)))
    root = list(range(len(items)))

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    by_num: dict = {}
    for i, it in enumerate(items):
        by_num.setdefault(it[0], []).append(i)
    for num, idx in by_num.items():
        if rec_count.get(num, 0) > 1:
            continue
        for a, i in enumerate(idx):
            for j in idx[a + 1:]:
                if _box_gap(items[i][1], items[j][1]) <= _TOUCH_MM:
                    root[find(i)] = find(j)
    members: dict = {}
    for i, it in enumerate(items):
        members.setdefault(find(i), []).append(it)
    lands = []
    for ms in members.values():
        x0 = min(m[1][0] for m in ms)
        y0 = min(m[1][1] for m in ms)
        x1 = max(m[1][2] for m in ms)
        y1 = max(m[1][3] for m in ms)
        views = [v for m in ms for v in m[2]]
        if len(ms) > 1:
            views.insert(0, ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0))
        lands.append({"number": ms[0][0], "views": views,
                      "area": (x1 - x0) * (y1 - y0)})
    return lands, _BIG_PAD * _median_area(
        [(b[2] - b[0]) * (b[3] - b[1]) for _, b, _ in items])


class _Grid:
    """The footprint's views (x, y, w, h, land index) in square cells, for
    nearest-view lookups that don't scan every pad."""

    def __init__(self, views: list, cell: float):
        self.cell = cell
        self.cells: dict = {}
        for v in views:
            key = (math.floor(v[0] / cell), math.floor(v[1] / cell))
            self.cells.setdefault(key, []).append(v)
        xs = [k[0] for k in self.cells]
        ys = [k[1] for k in self.cells]
        self.box = (min(xs), min(ys), max(xs), max(ys))

    def _ring(self, qx: int, qy: int, r: int):
        """The cells r steps from (qx, qy) (a square ring), inside the box."""
        x0, y0, x1, y1 = self.box
        if r == 0:
            yield qx, qy
            return
        lo, hi = max(qx - r, x0), min(qx + r, x1)
        for j in (qy - r, qy + r):
            if y0 <= j <= y1:
                for i in range(lo, hi + 1):
                    yield i, j
        lo, hi = max(qy - r + 1, y0), min(qy + r - 1, y1)
        for i in (qx - r, qx + r):
            if x0 <= i <= x1:
                for j in range(lo, hi + 1):
                    yield i, j

    def near(self, x: float, y: float, slack: float = _HERE_MM) -> tuple[float, list]:
        """(the nearest view's distance from (x, y), the views no more than
        `slack` farther). Rings of cells outward from the point's, from the
        first that reaches the box; a ring r steps out is at least
        (r - 1) cells away, so the walk stops once that passes the best
        distance plus the slack."""
        c = self.cell
        qx, qy = math.floor(x / c), math.floor(y / c)
        x0, y0, x1, y1 = self.box
        r0 = max(0, x0 - qx, qx - x1, y0 - qy, qy - y1)
        rmax = max(qx - x0, x1 - qx, qy - y0, y1 - qy)
        best = math.inf
        found = []
        for r in range(r0, rmax + 1):
            for key in self._ring(qx, qy, r):
                for v in self.cells.get(key, ()):
                    d = math.hypot(v[0] - x, v[1] - y)
                    if d <= best + slack:
                        found.append((d, v))
                        best = min(best, d)
            if best + slack < r * c:
                break
        return best, [v for d, v in found if d <= best + slack]


class _Fit:
    """What every placement of one record on one footprint shares."""

    def __init__(self, pads: list, rec: list[dict], spacing: float, tol: float,
                 size_tol: float = 0.1):
        self.rec = rec
        self.tol = tol
        self.size_tol = size_tol
        self.rec_count = Counter(r["number"] for r in rec)
        self.lands, self.big_area = _fp_lands(pads, self.rec_count)
        self.fp_nums = {g["number"] for g in self.lands}
        views = [v + (gi,) for gi, g in enumerate(self.lands) for v in g["views"]]
        self.grid = _Grid(views, min(5.0, max(0.2, spacing)))
        self.by_num: dict = {}
        for v in views:
            self.by_num.setdefault(self.lands[v[4]]["number"], []).append(v)
        self.xy = [tuple(p.copper_center) for p in pads]
        self.xy_shared = [tuple(p.copper_center) for p in pads
                          if p.number.strip() in self.rec_count]


def _centre_shifts(fp_xy: list, pts: list) -> list[tuple[float, float]]:
    """Shifts that put the points' bounding-box centre, and their centroid,
    on the pads'."""
    if not fp_xy or not pts:
        return []
    fx, fy = [p[0] for p in fp_xy], [p[1] for p in fp_xy]
    rx, ry = [p[0] for p in pts], [p[1] for p in pts]
    return [((min(fx) + max(fx) - min(rx) - max(rx)) / 2,
             (min(fy) + max(fy) - min(ry) - max(ry)) / 2),
            (sum(fx) / len(fx) - sum(rx) / len(rx),
             sum(fy) / len(fy) - sum(ry) / len(ry))]


def _vote_shifts(fit: _Fit, pts: list, top: int = 3) -> list[tuple[float, float]]:
    """Shifts the pin numbers vote for: each record pin, for each view of
    the lands with its number, votes once for the cell (tol / 2 square)
    that shift falls in. The `top` cells' mean shifts come back. Finds the
    fit when the record leaves out pads (mounting lugs, a shield) or lists
    pins the footprint lacks, which pull the centres off. A number with
    more than 2500 pin-view pairs (a pad grid sharing one name) sits out."""
    step = fit.tol / 2
    cells: dict = {}
    for i, (r, (px, py)) in enumerate(zip(fit.rec, pts)):
        views = fit.by_num.get(r["number"])
        if not r["number"] or not views or \
                fit.rec_count[r["number"]] * len(views) > 2500:
            continue
        for v in views:
            sx, sy = v[0] - px, v[1] - py
            cell = cells.setdefault((math.floor(sx / step), math.floor(sy / step)),
                                    ([], set()))
            cell[0].append((sx, sy))
            cell[1].add(i)
    ranked = sorted(cells.items(), key=lambda kv: (-len(kv[1][1]), kv[0]))[:top]
    return [(sum(s[0] for s in shifts) / len(shifts),
             sum(s[1] for s in shifts) / len(shifts))
            for _, (shifts, _) in ranked]


def _refine(fit: _Fit, pts: list, shift: tuple[float, float]) -> tuple[float, float]:
    """Move the shift to the mean offset of each pin's nearest view, up to
    five times, until it stops moving."""
    sx, sy = shift
    for _ in range(5):
        dx = dy = 0.0
        for px, py in pts:
            qx, qy = px + sx, py + sy
            _, here = fit.grid.near(qx, qy, slack=0.0)
            v = min(here, key=lambda u: math.hypot(u[0] - qx, u[1] - qy))
            dx += v[0] - px
            dy += v[1] - py
        nx, ny = dx / len(pts), dy / len(pts)
        moved = abs(nx - sx) >= 1e-6 or abs(ny - sy) >= 1e-6
        sx, sy = nx, ny
        if not moved:
            break
    return sx, sy


def _score(fit: _Fit, pts: list, shift: tuple[float, float]) -> dict:
    """One placement judged: the cost (the worst pin's distance to its
    nearest view, plus half of what the closest size difference there,
    either way round, exceeds size_tol by, when the record gives sizes) and
    each pin that misses its number, sorted into mismatches, renames and
    ties:

      rename   the pin's name is no footprint pad's and the pads here carry
               no record pin's number (the record's EP on pad 9, pin 9 on
               an unnumbered pad)
      shared   a tie: the pin's name is no footprint pad's, the land here is
               big (a tab, an exposed pad) and every record number on it is
               a pin that sits right elsewhere (the record's EP = 17 on a
               QFN's pad 4: the footprint ties the pad to pin 4)
      repeated a tie: the record lists the pin more than once and it sits
               right elsewhere; the pads here carry no record pin's number
               (a tab the record calls pin 2, drawn as pad 4 or unnumbered)

    Anything else is a mismatch."""
    sx, sy = shift
    cost = 0.0
    spots = []
    for r, (px, py) in zip(fit.rec, pts):
        qx, qy = px + sx, py + sy
        dmin, here = fit.grid.near(qx, qy)
        c = dmin
        w, h = r["w"], r["h"]
        if w is not None and h is not None:
            c += 0.5 * max(0.0, min(min(max(abs(v[2] - w), abs(v[3] - h)),
                                        max(abs(v[2] - h), abs(v[3] - w)))
                                    for v in here) - fit.size_tol)
        cost = max(cost, c)
        gis = {v[4] for v in here}
        spots.append((r["number"], qx, qy,
                      sorted({fit.lands[g]["number"] for g in gis}),
                      max(fit.lands[g]["area"] for g in gis)))
    matched = {pin for pin, _, _, nums, _ in spots if pin in nums}
    mismatches: list = []
    renames: list = []
    ties: list = []
    for pin, qx, qy, nums, area in spots:
        if pin in nums:
            continue
        spot = {"pin": pin, "at": (round(qx, 4), round(qy, 4)), "pads": nums}
        on_rec = [n for n in nums if n in fit.rec_count]
        if pin not in fit.fp_nums and not on_rec:
            renames.append(spot)
        elif pin not in fit.fp_nums and area > 0 and area >= fit.big_area and \
                all(n in matched for n in on_rec):
            ties.append(dict(spot, rule="shared"))
        elif fit.rec_count[pin] >= 2 and pin in matched and not on_rec:
            ties.append(dict(spot, rule="repeated"))
        else:
            mismatches.append(spot)
    return {"cost": cost, "shift": (sx, sy), "mismatches": mismatches,
            "renames": renames, "ties": ties}


def _number_turn(fit: _Fit, mirror: bool) -> tuple | None:
    """The turn, in degrees counter-clockwise on screen, that best puts the
    record's pins (mirrored first, if `mirror`) on the lands carrying their
    numbers (least squares: the Kabsch angle), with the centroids of those
    pins and lands: (deg, (x, y), (x, y)). Uses the pins whose number the
    record lists once and one land carries. None with fewer than two, with
    either side all at one spot, or when the numbers point to no angle (a
    ring numbered the other way round)."""
    land_at: dict = {}
    for g in fit.lands:
        n = g["number"]
        if n:
            land_at[n] = None if n in land_at else g["views"][0][:2]
    pairs = [((r["x"], r["y"]), land_at[r["number"]]) for r in fit.rec
             if fit.rec_count[r["number"]] == 1 and land_at.get(r["number"])]
    if len(pairs) < 2:
        return None
    n = len(pairs)
    ma = (sum(a[0] for a, _ in pairs) / n, sum(a[1] for a, _ in pairs) / n)
    mb = (sum(b[0] for _, b in pairs) / n, sum(b[1] for _, b in pairs) / n)
    dot = cross = span_a = span_b = 0.0
    for (ax, ay), (bx, by) in pairs:
        ax, ay, bx, by = ax - ma[0], ay - ma[1], bx - mb[0], by - mb[1]
        if mirror:
            ax = -ax
        dot += ax * bx + ay * by
        cross += bx * ay - by * ax
        span_a = max(span_a, math.hypot(ax, ay))
        span_b = max(span_b, math.hypot(bx, by))
    if span_a <= 1e-3 or span_b <= 1e-3 or math.hypot(dot, cross) <= 1e-9:
        return None
    return math.degrees(math.atan2(cross, dot)), ma, mb


def _angle_places(fit: _Fit, mirror: bool) -> list[dict]:
    """Placements at the angle the pin numbers point to (_number_turn) and
    the three quarter turns from it (pads that fit a quarter turn from where
    the numbers do: a part numbered from the wrong corner), each also tried
    at the shift that puts the paired pins' centroid on the lands'. None
    when that angle is within 0.01 mm, at the pin farthest from the
    record's centroid, of a quarter turn: those are tried anyway."""
    got = _number_turn(fit, mirror)
    if got is None:
        return []
    deg, ma, mb = got
    cx = sum(r["x"] for r in fit.rec) / len(fit.rec)
    cy = sum(r["y"] for r in fit.rec) / len(fit.rec)
    reach = max(math.hypot(r["x"] - cx, r["y"] - cy) for r in fit.rec)
    off = abs(deg - 90.0 * round(deg / 90.0))
    if reach * math.radians(off) <= 0.01:
        return []
    out = []
    for j in range(4):
        d = deg + 90.0 * j
        px, py = _rotate(ma[0], ma[1], d, mirror)
        out.append(_place(fit, None, mirror, d, [(mb[0] - px, mb[1] - py)]))
    return out


def _place(fit: _Fit, k: int | None, mirror: bool, deg: float | None = None,
           extra: list | None = None) -> dict:
    """The best shift for the record turned (k quarter turns, or `deg`
    degrees when k is None; mirrored first if `mirror`) onto the footprint.
    Starts: none; `extra`; the bounding-box centres and the centroids, of
    all pins and pads and of those whose numbers both sides use; the shifts
    the pin numbers vote for. Each is tried as it is and refined. The
    lowest cost wins; near-equal costs go to fewer mismatches, then fewer
    ties.
    -> {k, turn_deg, mirrored, cost, shift, mismatches, renames, ties}"""
    if k is not None:
        deg = 90.0 * (k % 4)
    pts = [_rotate(r["x"], r["y"], deg, mirror) for r in fit.rec]
    starts = [(0.0, 0.0)] + list(extra or []) + _centre_shifts(fit.xy, pts) + _centre_shifts(
        fit.xy_shared, [p for p, r in zip(pts, fit.rec) if r["number"] in fit.fp_nums]
    ) + _vote_shifts(fit, pts)
    shifts: list = []
    for s in starts:
        for t in (s, _refine(fit, pts, s)):
            if not any(abs(t[0] - u[0]) <= 1e-4 and abs(t[1] - u[1]) <= 1e-4
                       for u in shifts):
                shifts.append(t)
    tried = [_score(fit, pts, s) for s in shifts]
    low = min(t["cost"] for t in tried)
    best = min((t for t in tried if t["cost"] <= low + 1e-4),
               key=lambda t: (len(t["mismatches"]), len(t["ties"]), t["cost"]))
    return {"k": k, "turn_deg": round(deg % 360.0, 2) % 360.0,
            "mirrored": mirror, "cost": round(best["cost"], 4),
            "shift": (round(best["shift"][0], 4), round(best["shift"][1], 4)),
            "mismatches": best["mismatches"], "renames": best["renames"],
            "ties": best["ties"]}


def _misplaced(fit: _Fit, places: list) -> list[str] | None:
    """For pads that fit the record only mirrored: the numbers of the pads
    whose pin sits more than tol from every land with its number, with the
    record laid straight where the most pins sit on their own lands (the
    pads a mirror moved; those on its line stay put). Each straight turn in
    `places` is laid at the shifts its pin numbers vote for (_vote_shifts):
    the most pins on their lands wins, then the smaller shift (the record
    nearest the footprint as drawn), then the earlier turn. None when no
    placement puts two pins on their lands, or puts every pin the footprint
    numbers there: nothing tells the pads apart."""
    best = None
    seen: set = set()
    for p in places:
        if p["mirrored"] or p["turn_deg"] in seen:
            continue
        seen.add(p["turn_deg"])
        pts = [_rotate(r["x"], r["y"], p["turn_deg"], False) for r in fit.rec]
        for sx, sy in _vote_shifts(fit, pts):
            on = {i for i, (r, (px, py)) in enumerate(zip(fit.rec, pts))
                  if any(math.hypot(v[0] - px - sx, v[1] - py - sy) <= fit.tol
                         for v in fit.by_num.get(r["number"], ()))}
            key = (-len(on), round(math.hypot(sx, sy), 4))
            if best is None or key < best[0]:
                best = (key, on)
    numbered = {i for i, r in enumerate(fit.rec) if r["number"] in fit.fp_nums}
    if best is None or len(best[1]) < 2 or best[1] >= numbered:
        return None
    return sorted({fit.rec[i]["number"] for i in numbered - best[1]})


def fit_record(pads, land_or_pads, size_tol: float = 0.1) -> dict | None:
    """Numbers against the record's top view (land_pattern.pads).

    `pads` are the footprint's copper pads (unnumbered ones take part as
    number ''); `land_or_pads` the record's land_pattern or record_pads()
    output. Returns None without per-pin data, else a dict with
      verdict  'pass' | 'numbering' (a wrong pin at some spot) |
               'unmatched' (the record's spots fit the pads at no turn
               tried, straight or mirrored) | 'off_axis' (no pass, and the
               record's pins share no row or column while the pads do: it
               looks turned by an angle that is not a quarter turn and
               not one its numbers point to, so neither a mismatch nor a
               misfit means anything) |
               'names_differ' (record and footprint share no pin
               name, under half the record's, or one while the footprint
               has numbers the record lacks: the record names its pins,
               say, and the footprint numbers them; nothing to compare)
      cause    for 'numbering': 'bottom_view_numbering' (the numbers fit the
               record mirrored), 'mirrored_footprint' (the pads only fit the
               record mirrored: drawn from a bottom view), 'numbering_rotated'
               (the numbers fit the record turned `turn_deg` degrees more
               than the pads do, counter-clockwise positive; `turns` is
               that in quarter turns, 1, -1 or 2, or None when it is no
               quarter turn), or 'numbers_differ'
      misplaced  for 'mirrored_footprint': the numbers of the pads off
               their pins with the record laid straight where the most pins
               sit on their own lands (_misplaced); None when that tells no
               pad apart
      placement  the record's placement on the footprint that was judged:
               {k (quarter turns, counter-clockwise; None for a placement
               at the angle the numbers point to), turn_deg (the turn in
               degrees, counter-clockwise, 0 to 360), mirrored, cost,
               shift, mismatches, renames, ties}
      mismatches  [{pin, at, pads}]: the record's pin at `at` lands on pads
               numbered `pads`
      renames  [{pin, at, pads}]: a pin whose name the footprint doesn't use,
               on pads whose numbers the record doesn't use (see _score);
               not counted against the footprint
      ties     [{pin, at, pads, rule}]: a pin on a pad the footprint gives
               another number, where a datasheet may tie the two ('shared',
               'repeated': see _score); not counted against the footprint,
               but worth a look
      cost, tol   worst pin-to-pad misfit of the placement shown (for
               'unmatched' and 'names_differ', of the best one) and the
               limit for a fit: 0.4 x the record's closest pin spacing, at
               least `size_tol` (fp_verify's --size-tol-mm). A record pin's
               size counts only by what it misses the pad's by beyond
               size_tol (see _score)

    The record is judged straight (turned, not mirrored): a placement fits
    when its cost is within tol and near the best straight one's; it passes
    when every pin sits on its number. Mirrored placements only name the
    cause, or say the pads fit only mirrored ('mirrored_footprint').

    Turns tried: the four quarter turns, and, when the pin numbers point to
    another angle (_number_turn: a record drawn turned, a round can's pins
    at 45 degrees), that angle and the quarter turns from it (_angle_places),
    each straight and mirrored. A turn the numbers don't point to is found
    only from a quarter turn: a record drawn at an odd angle onto a wrongly
    numbered footprint gives 'off_axis', not 'numbering'. A footprint
    numbered a turn of its own symmetry round (a square QFN a quarter turn,
    a ring of pins one pin on) fits the record turned, so it passes: the
    pads can't tell it from a turned part, and only its pin-1 mark could.

    The record's pin may meet a pad at its hole, its shape or its copper
    box, and touching pads with one number count as one land as well as
    pads (see _pad_views, _fp_lands). A passing placement with the fewest
    ties is shown."""
    rec = record_pads(land_or_pads) if isinstance(land_or_pads, dict) \
        else list(land_or_pads)
    pads = list(pads)
    if not rec or not pads:
        return None
    spacing = _min_spacing(rec)
    tol = max(size_tol, 0.4 * spacing)
    fit = _Fit(pads, rec, spacing, tol, size_tol)
    rec_nums = set(fit.rec_count)
    shared = rec_nums & fit.fp_nums
    out = {"verdict": None, "cost": None, "tol": round(tol, 4), "cause": None,
           "turns": None, "turn_deg": None, "mismatches": [], "renames": [],
           "ties": [], "misplaced": None,
           "placement": None, "shared_names": len(shared)}
    if not shared or len(shared) < 0.5 * len(rec_nums) or \
            (len(shared) < 2 and not (fit.fp_nums - {""}) <= rec_nums):
        out["verdict"] = "names_differ"
        return out
    places = [_place(fit, k, m) for m in (False, True) for k in range(4)]
    places += _angle_places(fit, False) + _angle_places(fit, True)
    out["cost"] = min(p["cost"] for p in places)

    def fitting(ps: list) -> list:
        """The placements in `ps` within tol and near the best of them: a
        turn that fits far worse than the best one is not a fit."""
        low = min(p["cost"] for p in ps)
        return [p for p in ps if p["cost"] <= min(tol, low + max(0.05, 0.5 * low))]
    straight = fitting([p for p in places if not p["mirrored"]])
    mirrored = fitting([p for p in places if p["mirrored"]])
    good = [p for p in straight if not p["mismatches"]]
    if good:
        best = min(good, key=lambda p: (len(p["ties"]), p["cost"],
                                        p["k"] is None, p["turn_deg"]))
        out.update(verdict="pass", placement=best, renames=best["renames"],
                   ties=best["ties"], cost=best["cost"])
        return out
    if _off_axis(pads, rec):
        out["verdict"] = "off_axis"
        return out
    if not straight and not mirrored:
        out["verdict"] = "unmatched"
        return out
    shown = min(straight or mirrored,
                key=lambda p: (p["cost"], len(p["mismatches"])))
    out.update(verdict="numbering", placement=shown, cost=shown["cost"],
               mismatches=shown["mismatches"], renames=shown["renames"],
               ties=shown["ties"])
    numbered_as = [p for p in places if not p["mismatches"] and p["cost"] <= tol]
    if not straight:
        out["cause"] = "mirrored_footprint"
        out["misplaced"] = _misplaced(fit, places)
    elif any(p["mirrored"] for p in numbered_as):
        out["cause"] = "bottom_view_numbering"
    elif numbered_as:
        turned = min(numbered_as, key=lambda p: p["cost"])
        out["cause"] = "numbering_rotated"
        d = (turned["turn_deg"] - shown["turn_deg"]) % 360.0
        d = d - 360.0 if d > 180.0 else d
        q = round(d / 90.0)
        out["turn_deg"] = round(d, 1)
        out["turns"] = (2 if q == -2 else q) \
            if q and abs(d - 90.0 * q) <= 1.0 else None
    else:
        out["cause"] = "numbers_differ"
    return out
