"""check_fp_geometry.py - each placed footprint's pads against a drawing of
the part's land pattern: where each numbered pad sits (pitch, row spacing,
pin order, the pin-1 corner), its size and its hole (P8 verify suite).

check_fp_package asks whether the footprint is the part's PACKAGE (an 0402
part on an 0603 land, by name). This asks whether the footprint's own pads
are that package's: a footprint can carry the right name and still put pin 1
on the wrong corner, number its pads round the wrong side, sit its rows at
the wrong spacing or shrink a pad. fp_verify judges one library footprint
against a datasheet extract while the library is made. This judges every
footprint on a finished board, foreign ones included, against what the
footprint can be held to:

  1. reference/land_patterns.yaml: a part's land pattern read off its maker's
     package drawing (pad centres by number in the top view), each row with
     a cite. The part is found by its MPN, Value or LCSC field.
  2. KiCad's stock footprint of the same name. A stock name is a promise of
     its geometry (KLC: the name states the package, pitch and pad count),
     so a footprint called Package_SO:SOIC-8_3.9x4.9mm_P1.27mm whose pads are
     not the stock one's is either an edited copy or a different part.
  3. The LCSC/EasyEDA model of the part's LCSC number (the land pattern LCSC
     draws from the maker's drawing), fitted as cpl_verify fits it.
  4. The footprint's own name: the pitch (_P1.27mm) and the pin count
     (SOIC-8, PinHeader_2x05) it states.

Kinds (one per ref and source; the worst first):
  fp_pin_order_vs_drawing  error    pads land on the drawing's only with
  fp_pin_order_vs_stock    error    their numbers permuted (pin 1 on another
                                    corner, numbered round the wrong side)
  fp_pads_vs_drawing       error    a numbered pad is not where the drawing
  fp_pads_vs_stock         error    or the stock footprint puts it (pitch,
                                    row spacing), by more than a quarter of
                                    the pad pitch
  fp_pad_numbers_vs_stock  warning  the pad numbers differ from the stock
                                    footprint's (an extra or missing pin)
  fp_pad_small_vs_stock    warning  a pad under half the stock one's area
  fp_drill_small_vs_stock  warning  a hole more than 0.05 mm smaller than
                                    the stock one (the lead may not fit)
  fp_courtyard_small_vs_stock warning  the courtyard ends more than 0.5 mm
                                    inside the stock one's and another
                                    part's pad sits in the strip it leaves out
  fp_pin1_mark_vs_number   error    the footprint marks another pad as pin 1
                                    the way the stock one marks its pad 1
                                    (its one rect pad, the fab chamfer or
                                    dot): a symmetric header numbered from
                                    the other end, which the pad set alone
                                    can't tell. With no such mark, a
                                    connector that fits only turned, or an
                                    own library's renumbered copy, is not
                                    judged
  fp_pin_order_vs_model    warning  the LCSC model's pads land only with
  fp_pads_vs_model         warning  numbers permuted / not at all
  fp_pitch_vs_drawing      error    the pitch along a row of three or more
  fp_pitch_vs_stock        warning  pads is not the drawing's, the stock
  fp_pitch_vs_model        warning  footprint's or the LCSC model's, by more
                                    than 0.03 mm (a pitch is a datasheet
                                    fact no library varies; a short row a
                                    step off stays inside the quarter-pitch
                                    pad tolerance above)
  fp_pitch_vs_name         warning  no two pads at the pitch the name states
                                    (_P1.27mm, or EasyEDA's -P1.27-)
  fp_pins_vs_name          warning  pins the name counts are missing

Everything is in the footprint's own frame; a footprint on the bottom is
compared with its mirror image (the board stores a flipped footprint's pads
mirrored), and a copy whose origin moved is aligned on the pads' centroid,
so only the pads' geometry counts. A larger pad or hole than the stock one
is not a finding (a hand-solder or wave land), nor pad size against the
LCSC model (land patterns of one package differ in toe length).

A footprint no source covers is listed in not_judged, by why, rather than
passed: the land pattern was then checked against nothing.
A part with an LCSC number whose model is not cached says so (the gate never
fetches): fp_geometry_coverage --fill-cache fetches those into the shared
cache, and counts each source's share per board.

CLI: --pcb board.kicad_pcb [--live] [--easyeda-cache DIR] [--out report.json]
     --live fetches LCSC models not in the cache (else only cached ones are
     read). exit 0 pass / 1 violations / 2 error, per SPEC section 6.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import checklib  # noqa: E402

SCRIPT = "check_fp_geometry"
LAND_PATTERNS = HERE.parent / "reference" / "land_patterns.yaml"
SMALL_AREA_FRAC = 0.5  # a pad under half the stock one's area; older stock
                       # libraries' lands of one name differ by up to ~40 %
DRILL_TOL_MM = 0.05
PITCH_TOL_MM = 0.03
CRTYD_TOL_MM = 0.5   # older stock libraries' courtyards differ by up to 0.25 mm
PIN1_MARK_MM = 2.0   # a fab chamfer is a short diagonal (KLC: 1 mm or a quarter of the body)
PIN1_DOT_MM = 0.5    # a pin-1 dot's radius
CONNECTOR = re.compile(r"^(?:J|P|CN|CON|X)\d", re.I)
CONNECTOR_NAME = re.compile(r"PinHeader|PinSocket|IDC|Conn|USB|Header|Socket|Terminal|JST|Molex", re.I)


# ------------------------------------------------------------------ pads

@dataclass
class P:
    number: str
    x: float
    y: float
    w: float          # extent along the footprint's x
    h: float
    drill: float | None = None
    shape: str = ""   # rect, roundrect, circle, oval, custom...


@dataclass
class FP:
    ref: str
    fpid: str
    side: str
    at: tuple[float, float]
    layer: str
    fields: dict
    pads: list[P] = field(default_factory=list)
    crtyd: tuple | None = None
    rot: float = 0.0
    marks: list = field(default_factory=list)  # pin-1 marks, own frame

    @property
    def connector(self) -> bool:
        return bool(CONNECTOR.match(self.ref) or CONNECTOR_NAME.search(self.name))

    @property
    def name(self) -> str:
        return self.fpid.split(":", 1)[-1]

    @property
    def lib(self) -> str:
        return self.fpid.split(":", 1)[0] if ":" in self.fpid else ""


def _t(x):
    return x.value() if hasattr(x, "value") else x


def _kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and c and str(_t(c[0])) == name]


def _kid(n, name):
    k = _kids(n, name)
    return k[0] if k else None


def _nums(n):
    return [float(_t(v)) for v in (n or [])[1:] if isinstance(_t(v), (int, float))]


def _pads(node, fp_rot: float) -> list[P]:
    """Copper pads with a number, in the footprint's own frame. A board
    footprint's pad angle is absolute (it includes the footprint's turn);
    a library footprint's (fp_rot 0) is its own."""
    out = []
    for pad in _kids(node, "pad"):
        if len(pad) < 4:
            continue
        num, ptype = str(_t(pad[1])), str(_t(pad[2]))
        if not num or ptype == "np_thru_hole":
            continue
        layers = [str(_t(x)) for x in (_kid(pad, "layers") or [None])[1:]]
        if not any(ly.endswith(".Cu") for ly in layers):
            continue
        at, size = _nums(_kid(pad, "at")), _nums(_kid(pad, "size"))
        if len(at) < 2 or len(size) < 2:
            continue
        local = ((at[2] if len(at) > 2 else 0.0) - fp_rot) % 180.0
        w, h = size[0], size[1]
        if abs(local - 90.0) < 1.0:
            w, h = h, w
        elif min(local, 180.0 - local) >= 1.0:
            w = h = min(w, h)  # an odd angle: only the narrow side is sure
        dn = _nums(_kid(pad, "drill"))
        out.append(P(num, at[0], at[1], w, h, min(dn) if dn else None,
                     str(_t(pad[3]))))
    return out


class Crt(tuple):
    """(x0, y0, x1, y1), with .segs the outline's straight edges when it is
    drawn from lines, rects and polygons only (None with an arc or circle):
    a module's courtyard is a T (body plus antenna keepout), and the box
    round it holds strips beside the body that are not in it."""
    segs: list | None = None


def _inside(segs: list, x: float, y: float) -> bool:
    """Even-odd ray cast over the outline's edges: no chaining needed."""
    n = 0
    for (x0, y0), (x1, y1) in segs:
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            n += 1
    return n % 2 == 1


def _crtyd(node) -> tuple | None:
    """(x0, y0, x1, y1) of the footprint's courtyard in its own frame (a
    board footprint's graphics are stored footprint-local, like its pads)."""
    xs, ys, segs = [], [], []
    for kind in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        for g in _kids(node, kind):
            ly = str(_t((_kid(g, "layer") or [None, ""])[1]))
            if not ly.endswith("CrtYd"):
                continue
            pts = [_nums(_kid(g, k)) for k in ("start", "end", "mid", "center")]
            pts += [_nums(x) for x in _kids(_kid(g, "pts") or [], "xy")]
            q2 = [tuple(q[:2]) for q in pts if len(q) >= 2]
            if segs is None or kind in ("fp_circle", "fp_arc"):
                segs = None
            elif kind == "fp_line" and len(q2) == 2:
                segs.append((q2[0], q2[1]))
            elif kind == "fp_rect" and len(q2) == 2:
                (a, b), (c, d) = q2
                segs += [((a, b), (c, b)), ((c, b), (c, d)), ((c, d), (a, d)), ((a, d), (a, b))]
            elif kind == "fp_poly" and len(q2) >= 3:
                segs += list(zip(q2, q2[1:] + q2[:1]))
            else:
                segs = None
            if kind == "fp_circle":
                c, e = _nums(_kid(g, "center")), _nums(_kid(g, "end"))
                if len(c) >= 2 and len(e) >= 2:
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    pts += [[c[0] - r, c[1] - r], [c[0] + r, c[1] + r]]
            for q in pts:
                if len(q) >= 2:
                    xs.append(q[0])
                    ys.append(q[1])
    if not xs:
        return None
    c = Crt((min(xs), min(ys), max(xs), max(ys)))
    c.segs = segs or None
    return c


def _marks(node) -> list[tuple[float, float]]:
    """Where the footprint's fab and silk graphics mark a pin, in its own
    frame: the midpoint of a short diagonal Fab line (KLC's chamfered pin-1
    corner) and the centre of a small circle on Fab or silk (a pin-1 dot)."""
    out = []
    for kind in ("fp_line", "fp_circle"):
        for g in _kids(node, kind):
            ly = str(_t((_kid(g, "layer") or [None, ""])[1]))
            fab = ly.endswith(".Fab")
            if not (fab or ly.endswith(".SilkS")):
                continue
            if kind == "fp_line" and fab:
                a, b = _nums(_kid(g, "start")), _nums(_kid(g, "end"))
                if len(a) < 2 or len(b) < 2:
                    continue
                dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
                if (min(dx, dy) > 0.05 and 0.5 <= dx / dy <= 2.0
                        and math.hypot(dx, dy) <= PIN1_MARK_MM):
                    out.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
            elif kind == "fp_circle":
                c, e = _nums(_kid(g, "center")), _nums(_kid(g, "end"))
                if len(c) >= 2 and len(e) >= 2 and math.hypot(
                        e[0] - c[0], e[1] - c[1]) <= PIN1_DOT_MM:
                    out.append((c[0], c[1]))
    return out


def board_footprints(pcb: Path) -> list[FP]:
    import sexpdata
    tree = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    out = []
    for node in _kids(tree, "footprint"):
        at = _nums(_kid(node, "at"))
        props = {str(_t(p[1])): str(_t(p[2])) for p in _kids(node, "property")
                 if len(p) > 2}
        for t in _kids(node, "fp_text"):  # KiCad 6 keeps them as fp_text
            if len(t) > 2 and str(_t(t[1])) in ("reference", "value"):
                props.setdefault(str(_t(t[1])).title(), str(_t(t[2])))
        layer = str(_t((_kid(node, "layer") or [None, "F.Cu"])[1]))
        attrs = {str(_t(a)) for a in (_kid(node, "attr") or [])[1:]}
        if attrs & {"board_only", "virtual"}:
            continue
        rot = at[2] if len(at) > 2 else 0.0
        out.append(FP(props.get("Reference", "?"), str(_t(node[1])),
                      "bottom" if layer.startswith("B.") else "top",
                      (at[0], at[1]) if len(at) >= 2 else (0.0, 0.0), layer,
                      props, _pads(node, rot), _crtyd(node), rot, _marks(node)))
    return out


def library_footprint(path: Path) -> tuple[list[P], tuple | None]:
    import sexpdata
    node = sexpdata.loads(path.read_text(encoding="utf-8"))
    return _pads(node, 0.0), _crtyd(node)


def library_marks(path: Path) -> list[tuple[float, float]]:
    import sexpdata
    return _marks(sexpdata.loads(path.read_text(encoding="utf-8")))


def _marked(pads: list[P], marks: list) -> str | None:
    """The one pad every mark sits clearly nearest (under 0.7 of the
    next pad's distance), or None."""
    cen = _centres([p for p in pads if p.number])
    hit = set()
    for mx, my in marks:
        d = sorted((math.hypot(x - mx, y - my), k) for k, (x, y) in cen.items())
        if len(d) < 2 or d[0][0] >= 0.7 * d[1][0]:
            return None
        hit.add(d[0][1])
    return hit.pop() if len(hit) == 1 else None


def pin1_cue(pads: list[P], marks: list, spads: list[P], smarks: list):
    """(pad, how): the pad a footprint marks as pin 1, read only by a cue
    the stock footprint uses for its own pad 1 (its one rect pad among round
    ones; a fab chamfer or dot nearest pad 1). None if there is no such cue,
    or the cues disagree."""
    cues = []
    s1 = {p.shape for p in spads if p.number == "1"}
    rest = {p.shape for p in spads if p.number not in ("1", "")}
    if len(s1) == 1 and rest and not s1 & rest:
        shape = next(iter(s1))
        hit = sorted({p.number for p in pads if p.number and p.shape == shape})
        if len(hit) == 1 and not {p.shape for p in pads if p.number == hit[0]} - {shape}:
            cues.append((hit[0], f"its one {shape} pad"))
    if smarks and _marked(spads, smarks) == "1":
        m = _marked(pads, marks)
        if m:
            cues.append((m, "its pin-1 mark on fab or silk"))
    if len({c[0] for c in cues}) != 1:
        return None
    return cues[0][0], " and ".join(c[1] for c in cues)


# ------------------------------------------------------------- matching

def _centres(pads: list[P]) -> dict[str, tuple[float, float]]:
    acc: dict[str, list] = {}
    for p in pads:
        acc.setdefault(p.number, []).append((p.x, p.y))
    return {k: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v))
            for k, v in acc.items()}


def _turn(x, y, deg):
    a = math.radians(deg)
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))


def _frames(side: str, turns=(0,)) -> list:
    """How a footprint's own pads map onto a drawing's top view, with `turns`:
    as they are, or on the bottom with KiCad's flip undone. The board file
    holds a flipped footprint's pads with local y -> -y (cpl_verify's
    "Bottom side"); only that one mirror is undone, so a copy numbered as
    the part's mirror image is still found on either side."""
    if side == "bottom":
        return [((lambda x, y: (x, -y)), t) for t in turns]
    return [((lambda x, y: (x, y)), t) for t in turns]


def _err(ref: dict, got: dict, frame) -> tuple[float, str]:
    """Worst pad distance of `got` (by number) from `ref` after the frame
    and centroid alignment, and the pad it is at."""
    m, t = frame
    keys = sorted(set(ref) & set(got))
    g = {k: _turn(*m(*got[k]), t) for k in keys}
    rc = (sum(ref[k][0] for k in keys) / len(keys), sum(ref[k][1] for k in keys) / len(keys))
    gc = (sum(g[k][0] for k in keys) / len(keys), sum(g[k][1] for k in keys) / len(keys))
    worst = max(((math.hypot(g[k][0] - gc[0] - ref[k][0] + rc[0],
                             g[k][1] - gc[1] - ref[k][1] + rc[1]), k) for k in keys))
    return worst


def _set_fits(ref: dict, got: dict, frame, tol: float) -> bool:
    """Every pad of `got` lands on a distinct pad of `ref`, numbers aside."""
    m, t = frame
    g = [_turn(*m(*v), t) for v in got.values()]
    r = list(ref.values())
    rc = (sum(x for x, _ in r) / len(r), sum(y for _, y in r) / len(r))
    gc = (sum(x for x, _ in g) / len(g), sum(y for _, y in g) / len(g))
    used = set()
    for x, y in g:
        hit = next((i for i, q in enumerate(r) if i not in used and math.hypot(
            x - gc[0] - q[0] + rc[0], y - gc[1] - q[1] + rc[1]) <= tol), None)
        if hit is None:
            return False
        used.add(hit)
    return True


def pitch(centres: dict) -> float:
    pts = list(centres.values())
    d = [math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(pts) for b in pts[i + 1:]]
    d = [x for x in d if x > 1e-3]
    return min(d) if d else 0.0


def compare(ref: dict, got: dict, side: str, turns=(0,)) -> dict:
    """{fit: True} or {fit: False, worst_mm, pad, permuted, tol}. `ref` and
    `got` are number -> centre over the same numbers."""
    tol = max(0.05, 0.25 * pitch(ref))
    best = min((_err(ref, got, f) + (f,) for f in _frames(side, turns)),
               key=lambda e: e[0])
    if best[0] <= tol:
        return {"fit": True, "tol": tol}
    permuted = len(ref) >= 2 and any(_set_fits(ref, got, f, tol)
                                     for f in _frames(side, turns))
    return {"fit": False, "worst_mm": round(best[0], 3), "pad": best[1],
            "permuted": permuted, "tol": round(tol, 3)}


# --------------------------------------------------------------- sources

def kicad_footprint_dir() -> Path | None:
    hits = []
    for k, v in os.environ.items():
        m = re.match(r"^KICAD(\d*)_FOOTPRINT_DIR$", k)
        if m and v:
            hits.append((int(m.group(1) or -1), v))
    for _, v in sorted(hits, key=lambda kv: -kv[0]):
        if Path(v).is_dir():
            return Path(v)
    try:
        import env
        cli = env.find_kicad_cli()
    except Exception:  # noqa: BLE001 - no KiCad: the stock source is off
        cli = None
    if cli is None:
        return None
    root = Path(cli).parents[1]
    for cand in (root / "share" / "kicad" / "footprints",
                 root / "usr" / "share" / "kicad" / "footprints"):
        if cand.is_dir():
            return cand
    return None


class Stock:
    def __init__(self, root: Path | None):
        self.root = root
        self.index: dict[str, list[Path]] = {}
        if root:
            for d in root.glob("*.pretty"):
                for f in d.glob("*.kicad_mod"):
                    self.index.setdefault(f.stem, []).append(f)
        self._cache: dict[Path, tuple] = {}

    def find(self, fp: FP) -> Path | None:
        hits = self.index.get(fp.name) or []
        same = [h for h in hits if h.parent.stem == fp.lib]
        if same:
            return same[0]
        return hits[0] if len(hits) == 1 else None

    def load(self, path: Path) -> tuple[list[P], tuple | None]:
        if path not in self._cache:
            self._cache[path] = library_footprint(path)
        return self._cache[path]

    def marks(self, path: Path) -> list:
        if ("marks", path) not in self._cache:
            self._cache[("marks", path)] = library_marks(path)
        return self._cache[("marks", path)]


def load_land_patterns(path: Path = LAND_PATTERNS) -> list[dict]:
    if not path.is_file():
        return []
    import yaml
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("parts") or []
    for r in rows:
        r["_re"] = re.compile(r["match"], re.I) if r.get("match") else None
        r["_fp"] = re.compile(r["footprint"]) if r.get("footprint") else None
    return rows


_PART_FIELDS = ("MPN", "Manufacturer Part Number", "MFR Part", "Part Number",
                "PartNumber", "Value", "LCSC", "LCSC Part", "JLCPCB Part #")


def land_row(fp: FP, rows: list[dict]) -> dict | None:
    """The row for this footprint: one matched by its part (regex on the
    MPN/Value/LCSC field) first, else a package row (`footprint`: a regex
    the whole footprint name, library aside, must match; a generic land
    such as a maker's 0603 chip pattern, for parts with no MPN to go by)."""
    for r in rows:
        if r.get("_re") is None or (r.get("_fp") and not r["_fp"].fullmatch(fp.name)):
            continue
        for k in _PART_FIELDS:
            v = fp.fields.get(k, "")
            if v and r["_re"].search(v):
                return r
    for r in rows:
        if r.get("_re") is None and r.get("_fp") and r["_fp"].fullmatch(fp.name):
            return r
    return None


def lcsc_of(fp: FP) -> str | None:
    for k, v in fp.fields.items():
        if re.search(r"lcsc|jlc", k, re.I) and re.fullmatch(r"C\d{2,}", (v or "").strip()):
            return v.strip()
    return None


_COUNT = re.compile(
    r"^(?:[A-Za-z]*-)?(SOIC|SOP|SSOP|TSSOP|MSOP|HTSSOP|QSOP|VSSOP|TSOP|DIP|"
    r"QFN|DFN|VQFN|WQFN|UQFN|TQFN|LQFP|TQFP|QFP|VSON|WSON|SON|PLCC|HVQFN|"
    r"LFCSP|SOT|TSOT)-(\d+)(?=[_-]|$)")
_GRID = re.compile(r"^(?:PinHeader|PinSocket|IDC-Header)_(\d+)x(\d+)_P")
_PITCH = re.compile(r"_P(\d+(?:\.\d+)?)mm|[-_]P(\d+\.\d+)(?=[-_]|$)")


def row_gaps(centres: dict, tol: float = 0.02) -> list[float]:
    """The smallest spacing between neighbours of each row of three or more
    pads (pads sharing an x or a y), one per row."""
    pts = list(centres.values())
    rows = set()
    for a in (0, 1):
        for p in pts:
            row = tuple(sorted(round(q[a], 3) for q in pts if abs(q[1 - a] - p[1 - a]) <= tol))
            if len(row) >= 3:
                rows.add((a, row))
    out = []
    for _, row in sorted(rows):
        gaps = [y - x for x, y in zip(row, row[1:]) if y - x > 1e-3]
        if gaps:
            out.append(min(gaps))
    return out


def pin_run_gaps(centres: dict, tol: float = 0.02) -> list[float]:
    """Neighbour spacings within runs of three or more pads in a row (same x
    or same y) whose numbers count on by one along it: the pins of one side.
    A centred exposed pad, a mounting tab or a missing pin breaks the run,
    so only pin-to-pin spacings are returned."""
    num = {k: int(k) for k in centres if k.isdigit()}
    out = set()
    for a in (0, 1):
        seen = set()
        for k in num:
            row = sorted((centres[j][a], num[j]) for j in num
                         if abs(centres[j][1 - a] - centres[k][1 - a]) <= tol)
            key = tuple(n for _, n in row)
            if len(row) < 3 or key in seen:
                continue
            seen.add(key)
            i = 0
            while i < len(row) - 1:
                step = row[i + 1][1] - row[i][1]
                j = i
                while j < len(row) - 1 and row[j + 1][1] - row[j][1] == step and abs(step) == 1:
                    j += 1
                if j - i >= 2:
                    out.update(round(row[t + 1][0] - row[t][0], 4) for t in range(i, j))
                i = max(j, i + 1)
    return sorted(out)


def row_pitch(centres: dict, tol: float = 0.02) -> float | None:
    """The smallest spacing between neighbours in a row of three or more
    pads, or None with no such row. Rows, not any two pads: a library may
    move a package's two rows closer or apart (toe length), never the pitch
    along one."""
    g = row_gaps(centres, tol)
    return min(g) if g else None


def pitch_off(ref: dict, got: dict) -> tuple[float, float] | None:
    """(reference row pitch, board row pitch) when they differ by more than
    PITCH_TOL_MM; None when they agree or either has no row of three."""
    r, b = row_pitch(ref), row_pitch(got)
    if r is None or b is None or abs(r - b) <= PITCH_TOL_MM:
        return None
    return r, b


def name_findings(fp: FP, centres: dict) -> list[tuple[str, str, dict]]:
    """(kind, msg, extra) from what the footprint's own name states."""
    out = []
    m = _PITCH.search(fp.name)
    # only a footprint with a row of three or more pads holds its pitch
    # between neighbours: a SOT-23-3 or SOT-89 names the lead pitch of a row
    # with its middle pin left out (pins 1 and 3 sit two pitches apart), and
    # a 4-pad land names a body dimension, not two pads
    if m and len(centres) >= 3 and row_gaps(centres):
        want = float(m.group(1) or m.group(2))
        pts = list(centres.values())
        d = [math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(pts) for b in pts[i + 1:]]
        # any two pads at the pitch: a footprint with a second, wider pitch
        # (a connector's tabs, #1066) still holds the one its name states
        if d and not any(abs(x - want) <= PITCH_TOL_MM for x in d):
            near = min(d, key=lambda x: abs(x - want))
            out.append(("fp_pitch_vs_name",
                        f"its name states a {want:g} mm pitch, but no two of its pads "
                        f"are {want:g} mm apart (the nearest spacing is {near:.3f} mm)",
                        {"name_pitch_mm": want, "nearest_mm": round(near, 3)}))
        else:
            # each run of pins numbered in order along a row holds the pitch:
            # one side of a QFP drawn at another pitch while the others keep it
            bad = [x for x in pin_run_gaps(centres) if abs(x - want) > PITCH_TOL_MM]
            if bad:
                out.append(("fp_pitch_vs_name",
                            f"its name states a {want:g} mm pitch, but pins numbered in "
                            f"order along a row are {bad[0]:.3f} mm apart",
                            {"name_pitch_mm": want, "row_pitch_mm": round(bad[0], 3)}))
    n = None
    g = _GRID.search(fp.name)
    if g:
        n = int(g.group(1)) * int(g.group(2))
    else:
        c = _COUNT.search(fp.name)
        if c and c.group(1) not in ("SOT", "TSOT"):
            n = int(c.group(2))
    if n:
        ints = {int(k) for k in centres if k.isdigit()}
        missing = sorted(set(range(1, n + 1)) - ints)
        if missing and len(ints) >= 2:
            out.append(("fp_pins_vs_name",
                        f"its name counts {n} pins, but pad(s) "
                        f"{', '.join(map(str, missing[:8]))} are missing",
                        {"name_pins": n, "missing": missing}))
    return out


def name_judges(fp: FP, centres: dict) -> bool:
    """True when name_findings can judge something: a pitch with a row of
    three pads to hold it, or a pin count (SOT counts are not judged)."""
    if _PITCH.search(fp.name) and len(centres) >= 3 and row_gaps(centres):
        return True
    if _GRID.search(fp.name):
        return True
    c = _COUNT.search(fp.name)
    return bool(c and c.group(1) not in ("SOT", "TSOT"))


# ----------------------------------------------------------------- check

def check(pcb: Path, live: bool = False, easyeda_cache: Path | None = None,
          stock: Stock | None = None, rows: list[dict] | None = None,
          trace: list | None = None):
    """(violations, judged by first source, not_judged by why). trace, if a
    list, gets one row per footprint: every source it was judged against, or
    why not (fp_geometry_coverage reads it)."""
    stock = stock or Stock(kicad_footprint_dir())
    rows = load_land_patterns() if rows is None else rows
    viols, judged, not_judged = [], Counter(), {}

    def add(fp, sev, kind, msg, **extra):
        viols.append(checklib.violation(
            SCRIPT, sev, list(fp.at), fp.layer, None, [fp.ref],
            f"{fp.ref} ({fp.fpid}): {msg}", SCRIPT, kind=kind,
            footprint=fp.fpid, **extra))

    cfps = None
    fps = board_footprints(pcb)
    for fp in fps:
        got = _centres(fp.pads)
        uncached = False  # its LCSC model was looked for and is not cached

        def note(sources, why=None):
            if trace is not None:
                trace.append({"ref": fp.ref, "fpid": fp.fpid, "fields": dict(fp.fields),
                              "lcsc": lcsc_of(fp), "pads": len(got), "sources": list(sources), "why": why,
                              "model_uncached": uncached})
        if len(got) < 2:
            note([], "under two numbered pads")
            continue
        sources, skip = [], None
        row = land_row(fp, rows)
        if row:
            ref = {str(k): tuple(v) for k, v in row["pads"].items()}
            common = set(ref) & set(got)
            if len(common) >= 2:
                sources.append("drawing")
                c = compare({k: ref[k] for k in common}, {k: got[k] for k in common},
                            fp.side, turns=(0, 90, 180, 270))
                if not c["fit"]:
                    kind = "fp_pin_order_vs_drawing" if c["permuted"] else "fp_pads_vs_drawing"
                    how = ("its pads land on the drawing's only with their numbers "
                           "permuted (pin 1 on another corner, or numbered round the "
                           "other side)" if c["permuted"] else
                           f"pad {c['pad']} is {c['worst_mm']} mm from where the drawing puts it")
                    add(fp, "error", kind,
                        f"against the land pattern of {row['part']} ({row['source']}): {how}. "
                        f"Fix: renumber or redraw the footprint to the drawing's top view",
                        drawing=row["source"], **{k: c[k] for k in ("worst_mm", "pad", "tol")})
                elif off := pitch_off({k: ref[k] for k in common}, {k: got[k] for k in common}):
                    add(fp, "error", "fp_pitch_vs_drawing",
                        f"its pads are {off[1]:.3f} mm apart along a row where the land "
                        f"pattern of {row['part']} ({row['source']}) has {off[0]:.3f} mm. "
                        f"Fix: redraw the footprint to the drawing's pitch",
                        drawing=row["source"], pitch_mm=round(off[0], 3),
                        board_pitch_mm=round(off[1], 3))
        sp = stock.find(fp)
        if sp is not None:
            sources.append("stock")
            spads, scrt = stock.load(sp)
            sref = _centres(spads)
            extra_n = sorted(set(got) - set(sref))
            miss_n = sorted(set(sref) - set(got))
            if _numbers_differ(extra_n, miss_n):
                add(fp, "warning", "fp_pad_numbers_vs_stock",
                    f"its pad numbers are not the stock {sp.parent.stem}:{sp.stem}'s"
                    + (f"; not in stock: {', '.join(extra_n[:8])}" if extra_n else "")
                    + (f"; missing: {', '.join(miss_n[:8])}" if miss_n else ""),
                    stock=str(sp.name), extra_pads=extra_n, missing_pads=miss_n)
            common = set(sref) & set(got)
            if len(common) >= 2:
                r_, g_ = {k: sref[k] for k in common}, {k: got[k] for k in common}
                # any turn: a pattern symmetric under a half turn (a 6 mm
                # tact switch) numbered either way round is the same land
                c = compare(r_, g_, fp.side, turns=(0, 90, 180, 270))
                if not c["fit"] and not c["permuted"] and _land_variant(
                        fp, spads, common, g_, r_):
                    c = {"fit": True}
                own = bool(fp.lib) and fp.lib != sp.parent.stem
                # pin order the pad set can't tell: a symmetric pattern that
                # fits only turned (a 1xN header numbered from the other
                # end), or an own library's renumbered copy. The stock's
                # pin-1 cue, where the footprint carries it, says which end
                cue = turned = None
                if fp.connector and c["fit"]:
                    turned = not compare(r_, g_, fp.side)["fit"]
                if turned or (not c["fit"] and c["permuted"] and own):
                    cue = pin1_cue(fp.pads, fp.marks, spads, stock.marks(sp))
                    if cue and cue[0] == "1":
                        cue = None
                    elif cue is None and turned:
                        skip = ("a connector whose pads fit the stock's only turned "
                                "(numbered from the other end) and that carries no "
                                "pin-1 mark to say which end is pin 1")
                if not c["fit"] and c["permuted"] and fp.connector and compare(
                        r_, g_, "top" if fp.side == "bottom" else "bottom",
                        turns=(0, 90, 180, 270))["fit"]:
                    skip = ("a connector numbered as the stock one's mirror image: "
                            "which hand mates is check_mate_pins' to judge")
                elif cue:
                    add(fp, "error", "fp_pin1_mark_vs_number",
                        f"{cue[1]} marks pad {cue[0]} as pin 1, where the stock "
                        f"{sp.parent.stem}:{sp.stem} marks pad 1 the same way: the pads "
                        f"are numbered from the other end. Fix: renumber the pads so the "
                        f"marked one is 1", stock=str(sp.name), pad=cue[0])
                elif skip:
                    pass
                elif not c["fit"] and c["permuted"] and own:
                    skip = (f"its own library's copy of a stock name ({fp.lib}): the "
                            f"numbering of a {sp.stem} is the part's, not the stock's")
                elif not c["fit"]:
                    kind = "fp_pin_order_vs_stock" if c["permuted"] else "fp_pads_vs_stock"
                    how = ("its pads are the stock footprint's with their numbers "
                           "permuted" if c["permuted"] else
                           f"pad {c['pad']} is {c['worst_mm']} mm from where the stock "
                           f"footprint puts it")
                    add(fp, "warning" if own else "error", kind,
                        f"named as KiCad's stock {sp.parent.stem}:{sp.stem}, but {how}. "
                        f"Fix: use the stock footprint, or rename the edited copy for what it is",
                        stock=str(sp.name), **{k: c[k] for k in ("worst_mm", "pad", "tol")})
                elif off := pitch_off(r_, g_):
                    add(fp, "warning", "fp_pitch_vs_stock",
                        f"named as KiCad's stock {sp.parent.stem}:{sp.stem}, but its pads "
                        f"are {off[1]:.3f} mm apart along a row where the stock one's are "
                        f"{off[0]:.3f} mm", stock=str(sp.name), pitch_mm=round(off[0], 3),
                        board_pitch_mm=round(off[1], 3))
                else:
                    small, drill = _sizes(fp.pads, spads, common)
                    if small:
                        k, b, s = small
                        add(fp, "warning", "fp_pad_small_vs_stock",
                            f"pad {k} is {b[0]:.2f} x {b[1]:.2f} mm where the stock "
                            f"{sp.stem} has {s[0]:.2f} x {s[1]:.2f} mm",
                            stock=str(sp.name), pad=k)
                    short = _courtyard_short(fp, got, scrt, sref, fps)
                    if short:
                        side_, mm, who = short
                        add(fp, "warning", "fp_courtyard_small_vs_stock",
                            f"its courtyard ends {mm:.2f} mm inside the stock "
                            f"{sp.stem}'s on the {side_} side, and "
                            f"{', '.join(who[:4])} sit(s) in the strip it leaves out: "
                            f"check they clear the package", stock=str(sp.name),
                            short_mm=round(mm, 3), intruders=who)
                    if drill:
                        k, b, s = drill
                        add(fp, "warning", "fp_drill_small_vs_stock",
                            f"pad {k}'s hole is {b:.2f} mm where the stock {sp.stem}'s "
                            f"is {s:.2f} mm: check the lead fits", stock=str(sp.name), pad=k)
        lcsc = lcsc_of(fp)
        if lcsc and fp.connector:
            # LCSC's connector models number pins by their own convention
            # (a 2xN header round the body, not odd/even), so a misfit says
            # nothing about the land: no model compare, judged or not
            if not sources and not skip:
                skip = "a connector: LCSC's models number connectors by their own convention"
        elif lcsc and "drawing" not in sources and not skip:
            import cpl_verify
            import easyeda
            model = easyeda.get(lcsc, easyeda_cache, fetch=live)
            uncached = model is None
            if model is not None and ({q.number for q in model.pads} - {""}) != (set(got) - {""}):
                # the land numbers pins the part does not have (a Kelvin
                # shunt land of a 2-terminal resistor): a different land,
                # not a misplaced one
                if not sources:
                    skip = ("its pad numbers are not the LCSC model's (a Kelvin "
                            "or alternate land of the part)")
            elif model is not None:
                if cfps is None:
                    cfps = cpl_verify.board_footprints(pcb)
                cf = cfps.get(fp.ref)
                if cf is not None:
                    sources.append("model")
                    a = cpl_verify.analyse(cf, model)
                    if a["verdict"] == cpl_verify.PIN_MAP and re.search(r"swap", fp.name, re.I):
                        skip = ("its name declares its pins swapped against the "
                                "part's numbering (to suit its symbol)")
                        sources.remove("model")
                    elif a["verdict"] == cpl_verify.PIN_MAP:
                        add(fp, "warning", "fp_pin_order_vs_model",
                            f"the LCSC model of {lcsc} ({model.package}) lands on its pads "
                            f"only with the numbers permuted: check which pin is on which "
                            f"pad against the part's datasheet", lcsc=lcsc)
                    elif a["verdict"] != "no_fit" and (off := pitch_off(
                            _centres([q for q in model.pads if q.number]), got)):
                        add(fp, "warning", "fp_pitch_vs_model",
                            f"its pads are {off[1]:.3f} mm apart along a row where the LCSC "
                            f"model of {lcsc} ({model.package}) has {off[0]:.3f} mm: check "
                            f"the pitch against the datasheet", lcsc=lcsc,
                            pitch_mm=round(off[0], 3), board_pitch_mm=round(off[1], 3))
                    elif a["verdict"] == "no_fit":
                        add(fp, "warning", "fp_pads_vs_model",
                            f"the LCSC model of {lcsc} ({model.package}) does not land on "
                            f"its pads by number at any turn (tolerance {a['tol_mm']} mm): "
                            f"check the pitch, row spacing and pin 1 against the datasheet",
                            lcsc=lcsc)
        if skip:
            not_judged.setdefault(skip, []).append(fp.ref)
            note(sources, skip)
            continue
        if not sources:
            for kind, msg, extra in name_findings(fp, got):
                add(fp, "warning", kind, msg, **extra)
            if name_judges(fp, got):
                sources.append("name")
        if sources:
            judged[sources[0]] += 1
            note(sources)
        else:
            why = (("its LCSC model is not cached (not fetched yet, or "
                    "EasyEDA has none: fp_geometry_coverage --fill-cache)") if lcsc and not fp.connector else
                   "no land pattern row, stock footprint of its name, "
                   "LCSC number or pitch in its name")
            not_judged.setdefault(why, []).append(fp.ref)
            note(sources, why)
    return viols, dict(judged), not_judged


def _land_variant(fp: FP, spads: list[P], common: set, got: dict, ref: dict) -> bool:
    """True when every board pad's centre lies on the stock pad of its number
    (both placed on their centroid, as stored): the same land with another
    toe length, as KiCad's own library redrew it (fc980c U5: the 2017
    VSSOP-8's 0.8 mm pads 3.1 mm apart, today's 1.25 mm pads 2.8 mm apart).
    A lead lands on both. A row spacing or pitch that puts a pad off the
    stock copper is not this."""
    flip = -1 if fp.side == "bottom" else 1
    keys = sorted(common)
    gx = sum(got[k][0] for k in keys) / len(keys)
    gy = sum(got[k][1] for k in keys) / len(keys)
    rx = sum(ref[k][0] for k in keys) / len(keys)
    ry = sum(ref[k][1] for k in keys) / len(keys)
    by = {}
    for q in spads:
        by.setdefault(q.number, []).append(q)
    for k in keys:
        x, y = got[k][0] - gx + rx, (got[k][1] - gy) * flip + ry
        if not any(abs(x - q.x) <= q.w / 2 and abs(y - q.y) <= q.h / 2 for q in by.get(k, [])):
            return False
    return True


def _numbers_differ(extra: list, missing: list) -> bool:
    """A pad-number difference worth a warning: a numbered pin missing, or
    one added. A shield or mounting pad named otherwise than the stock one
    (S1 or 5, 6 for SH) is not."""
    if any(k.isdigit() for k in missing):
        return True
    if missing:
        return False
    return any(k.isdigit() for k in extra)


_DIHEDRAL = [(1, 0, 0, 1), (-1, 0, 0, 1), (1, 0, 0, -1), (-1, 0, 0, -1),
             (0, 1, 1, 0), (0, -1, 1, 0), (0, 1, -1, 0), (0, -1, -1, 0)]


def _courtyard_short(fp: FP, got: dict, scrt, sref: dict, fps=()):
    """(side, mm, refs) where the footprint's courtyard falls short of the
    stock one's by more than CRTYD_TOL_MM, both placed on their pads'
    centroid, and other parts' pads sit in the strip it leaves out; None
    otherwise, or if either has no courtyard (no courtyard at all is
    place_metrics' to judge). A short courtyard with nothing in the strip
    harms nothing."""
    if fp.crtyd is None or scrt is None:
        return None
    keys = sorted(set(got) & set(sref))
    bx0, by0, bx1, by1 = fp.crtyd
    flip = fp.side == "bottom"
    if flip:  # undo KiCad's flip, as for the pads
        by0, by1 = -by1, -by0
    cy = sum(got[k][1] for k in keys) / len(keys) * (-1 if flip else 1)
    cx = sum(got[k][0] for k in keys) / len(keys)
    sx = sum(sref[k][0] for k in keys) / len(keys)
    sy = sum(sref[k][1] for k in keys) / len(keys)
    sx0, sy0, sx1, sy1 = scrt
    # the stock courtyard under each turn or mirror about the pads' centre
    # that maps the stock pads' positions onto themselves, the closest kept:
    # a symmetric pad row (a 1x5 angled header) drawn with its body the
    # other way round (fc980c J1, over the board edge) is the stock land,
    # and its courtyard the stock's mirrored, not short
    corners = [(a - sx, b - sy) for a in (sx0, sx1) for b in (sy0, sy1)]
    rel = [(sref[k][0] - sx, sref[k][1] - sy) for k in keys]
    best = None
    for M in _DIHEDRAL:
        if not all(any(math.hypot(M[0] * u + M[1] * v - a, M[2] * u + M[3] * v - b) < 0.05
                       for a, b in rel) for u, v in rel):
            continue
        pts = [(M[0] * u + M[1] * v, M[2] * u + M[3] * v) for u, v in corners]
        S_ = (min(x for x, _ in pts) + cx, min(y for _, y in pts) + cy,
              max(x for x, _ in pts) + cx, max(y for _, y in pts) + cy)
        gaps = {"left": bx0 - S_[0], "top": by0 - S_[1],
                "right": S_[2] - bx1, "bottom": S_[3] - by1}
        side_, mm_ = max(gaps.items(), key=lambda kv: kv[1])
        if best is None or mm_ < best[0] - 1e-9:
            best = (mm_, side_, S_, M)
    mm, side, S, M = best
    if mm <= CRTYD_TOL_MM:
        return None
    who = []
    for o in fps:
        if o is fp or not o.pads:
            continue
        for q in o.pads:
            if q.drill is None and o.side != fp.side:
                continue
            gx, gy = _turn(q.x, q.y, o.rot)
            lx, ly = _turn(gx + o.at[0] - fp.at[0], gy + o.at[1] - fp.at[1], -fp.rot)
            if flip:
                ly = -ly
            inside = S[0] < lx < S[2] and S[1] < ly < S[3]
            if inside and getattr(scrt, "segs", None):
                u, v = lx - cx, ly - cy  # back through M (orthogonal: its transpose)
                inside = _inside(scrt.segs, M[0] * u + M[2] * v + sx, M[1] * u + M[3] * v + sy)
            mine = bx0 <= lx <= bx1 and by0 <= ly <= by1
            if inside and not mine:
                who.append(o.ref)
                break
    return (side, mm, who) if who else None


def _sizes(bpads: list[P], spads: list[P], common: set):
    """(pad, board w h, stock w h) of the first pad under half the area of the
    stock one; (pad, board drill, stock drill) of the first smaller hole."""
    def by(pads):
        out = {}
        for p in pads:
            out.setdefault(p.number, p)
        return out
    b, s = by(bpads), by(spads)
    small = drill = None
    for k in sorted(common, key=lambda x: (len(x), x)):
        bp, sp = b[k], s[k]
        # by area: a pad's w and h are not sure on a turned footprint
        if small is None and bp.w * bp.h < SMALL_AREA_FRAC * sp.w * sp.h:
            small = (k, (bp.w, bp.h), (sp.w, sp.h))
        if (drill is None and bp.drill is not None and sp.drill is not None
                and bp.drill < sp.drill - DRILL_TOL_MM):
            drill = (k, bp.drill, sp.drill)
    return small, drill


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--live", action="store_true",
                    help="fetch LCSC models not in the cache")
    ap.add_argument("--easyeda-cache")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    pcb = Path(args.pcb)
    if not pcb.is_file():
        raise checklib.CheckError(f"board not found: {pcb}")
    viols, judged, nj = check(pcb, args.live,
                              Path(args.easyeda_cache) if args.easyeda_cache else None)
    return checklib.report(
        SCRIPT, pcb, viols, judged=judged, not_judged=nj,
        nothing_to_judge=None if viols or sum(judged.values()) else
        "no footprint could be compared with a stock KiCad or LCSC model "
        "footprint"), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
