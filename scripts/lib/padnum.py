"""padnum.py - which way a perimeter package's pad numbers run (#1285).

fp_verify's pad_numbering check places each pin by the record's
land_pattern.pads. With no such list (or none that fits the pads), this says
whether the footprint's numbers run the way a top view does, ported from
#1286's padnum.py. Coordinates are KiCad's: millimetres, x right, y DOWN, the
footprint seen from the top.

  classify(fp_name, *record_names)  which package family a name says:
      'perimeter' (SOIC, SOT, DFN, QFN, LGA ...) or 'bga'; connectors,
      PLCCs, SOT-227 modules and names that declare their numbering
      (clockwise, reversed, swapped) are left alone.
  perimeter_direction(pads)  on a perimeter package, top-view numbers run
      counter-clockwise from pin 1; clockwise means the footprint was
      numbered from a bottom view.

A pad's place is its centre and its size its pad's size (fplib.Pad), so a
custom pad drawn off its anchor counts at its anchor. Pure python.
"""
import math
import re


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
        x, y = p.center
        w, h = p.size
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

