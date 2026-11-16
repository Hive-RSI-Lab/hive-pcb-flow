"""gerblib.py - gerber/drill -> shapely geometry (the INDEPENDENT path).

geom.py (S3) reads the .kicad_pcb. This module reads what we actually SHIP:
the exported gerbers + Excellon drill. Two different parsers over two different
file formats means an export-stage defect (a layer that did not make it into
the package, a mask expansion that ate an annular ring) shows up as a
disagreement instead of being invisible - that is the whole point of
dfm_check.py being a second geometry path (SPEC P9).

Coordinates: gerbers are emitted with Y pointing UP, the .kicad_pcb has Y
pointing DOWN. Everything this module returns is in BOARD space (y negated),
in millimetres, so DFM violations carry the same coordinates as every other
check in the pipeline (manifest.yaml, geom.py, kc.py).

Public API:
    open_fab(dir) -> FabStack
    FabStack.copper(layer)   -> LayerGeom  (traces, pads, pours, widths)
    FabStack.silk(side)      -> LayerGeom
    FabStack.mask(side)      -> LayerGeom  (flashes = mask OPENINGS)
    FabStack.paste(side)     -> LayerGeom  (flashes = stencil apertures)
    FabStack.outline         -> shapely Polygon | None (from Edge.Cuts)
    FabStack.holes           -> [Hole{x, y, diameter, plated}]
"""
from __future__ import annotations

import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from gerbonara import ExcellonFile, GerberFile
from gerbonara.graphic_objects import Arc, Flash, Line, Region
from gerbonara.graphic_primitives import Rectangle
from gerbonara.rs274x import GerberParser
from shapely import set_precision
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

# KiCad 10 writes `%TD.AperFunction*%` where no .AperFunction is set (minuet
# v3's F.Cu, line 508), and gerbonara refuses the whole file for it. There is
# nothing to delete, so deleting nothing is the right reading.
_parse_attribute = GerberParser._parse_attribute


def _tolerant_attribute(self, match):
    try:
        return _parse_attribute(self, match)
    except SyntaxError as e:
        if match["type"] != "TD" or "previously undefined" not in str(e):
            raise


GerberParser._parse_attribute = _tolerant_attribute

# kicad-cli names exports "<board>-<Layer_Name>.<ext>"; the layer token is what
# we key on so the board name never matters.
_COPPER_RE = re.compile(r"-(F_Cu|B_Cu|In\d+_Cu)\.(gtl|gbl|g\d+|gbr)$", re.I)
_SILK_RE = re.compile(r"-(F|B)_Silkscreen\.(gto|gbo|gbr)$", re.I)
_MASK_RE = re.compile(r"-(F|B)_Mask\.(gts|gbs|gbr)$", re.I)
_PASTE_RE = re.compile(r"-(F|B)_Paste\.(gtp|gbp|gbr)$", re.I)
_EDGE_RE = re.compile(r"-Edge_Cuts\.(gm1|gbr)$", re.I)
_DRILL_RE = re.compile(r"\.(drl|xln|txt)$", re.I)
# ...except that KiCad names the file after the layer's USER name, and copper
# layers are the ones users rename (KiCad 10 demo ecc83: "top_cu"/"bottom_cu"
# -> ecc83-pp-top_cu.gtl). The Protel extension still states the layer
# function, so it is the fallback key: .gtl=F.Cu, .gbl=B.Cu, .g<n>=In<n>.Cu
# (verified against this repo's own 4-layer exports). Silk/mask/paste/edge
# names cannot be renamed in KiCad, so only copper needs the fallback.
_COPPER_EXT_RE = re.compile(r"\.(?:(gtl)|(gbl)|g(\d+))$", re.I)
# Protel extensions alone, for gerbers named after a board's own layer names
# (an Altium import keeps "Top Solder", "Bottom Overlay"...): o = overlay
# (silk), s = solder mask, p = paste; t/b = top/bottom
_SIDE_EXT_RE = re.compile(r"\.g([tb])([osp])$", re.I)


def _layer_key(token: str) -> str:
    """'F_Cu' -> 'F.Cu' (pipeline layer naming)."""
    return token.replace("_", ".")


@dataclass
class Hole:
    x: float
    y: float
    diameter: float
    plated: bool = True


@dataclass
class LayerGeom:
    """One gerber layer's geometry, in board space."""
    name: str
    traces: list = field(default_factory=list)     # buffered polygons
    trace_widths: list = field(default_factory=list)
    trace_lines: list = field(default_factory=list)  # (LineString, width)
    pads: list = field(default_factory=list)       # flash polygons
    pours: list = field(default_factory=list)      # region polygons
    _union = None

    @property
    def all_polys(self) -> list:
        return self.traces + self.pads + self.pours

    def union(self):
        """Cached union of every feature on the layer."""
        if self._union is None:
            polys = self.all_polys
            self._union = unary_union(polys) if polys else Polygon()
        return self._union

    def components(self) -> list:
        """Electrically-distinct copper islands (union then explode). Gerbers
        carry no net data, so 'touching = same conductor' is the only honest
        grouping - which is exactly what a fab's DFM engine sees."""
        u = self.union()
        if u.is_empty:
            return []
        return list(u.geoms) if u.geom_type.startswith("Multi") else [u]

    @property
    def min_trace_width(self) -> float | None:
        return min(self.trace_widths) if self.trace_widths else None


# ArcPoly.outline carries only the segment ENDPOINTS - a circular pad's
# curvature lives in its arc segments, so taking .outline directly turns a
# round pad into a coarse few-sided polygon (measured: a 0.6 mm pad read as
# ~0.55 mm, i.e. a phantom 0.025 mm annular-ring shortfall). Tessellate arcs
# to 1 um before converting, well under every JLC capability threshold.
ARC_MAX_ERROR_MM = 1e-3

# Snap applied to Edge.Cuts linework before polygonize: kicad-cli's 4.6-format
# round-off leaves corner joints up to 1e-6 mm apart, polygonize has no
# tolerance of its own, and an "open" outline silently disabled both
# edge-distance checks (lumina-carrier retro 2026-08-07). 1e-5 mm is 10x the
# format resolution and 1/10000 of any fab tolerance.
OUTLINE_SNAP_MM = 1e-5

# A grid snap can't join two ends that straddle a grid line, however close.
# KiCad's Gerber writer recomputes a corner arc's end from its centre, so at
# a non-round origin (board_init's shelf-pack origin, x = 22.864999 on
# lite-main) the arc misses the straight edge by up to 0.05 um and the
# outline read as open, which turned off both edge checks (#858). So line
# ends within 0.1 um are joined first, to the end met first: twice the
# worst miss measured, and 1/1000 of any fab tolerance.
OUTLINE_JOIN_MM = 1e-4


def _join_ends(lines: list, tol: float = OUTLINE_JOIN_MM) -> list:
    """The lines with each end moved onto an earlier line end within tol
    (#858). A line whose ends then meet and that has no other point is
    dropped."""
    seen: dict[tuple[int, int], list] = {}

    def at(pt):
        i, j = math.floor(pt[0] / tol), math.floor(pt[1] / tol)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for q in seen.get((i + di, j + dj), ()):
                    if math.dist(q, pt) <= tol:
                        return q
        seen.setdefault((i, j), []).append(pt)
        return pt

    out = []
    for ls in lines:
        cs = list(ls.coords)
        cs[0], cs[-1] = at(cs[0]), at(cs[-1])
        if len(set(cs)) >= 2:
            out.append(LineString(cs))
    return out


def _rect_pts(r) -> list:
    """A gerbonara Rectangle's four corners. gerbonara 1.6.3's own
    Rectangle.to_arc_poly is right only at multiples of 90 degrees: at 45 or
    315 degrees a square comes out as four copies of its centre, so a KiCad
    RotRect pad (a square pad turned 45 degrees) vanished from the copper and
    a track into it read as 0.015 mm from its drill (minuet v4 J5, #454)."""
    c, s = math.cos(r.rotation), math.sin(r.rotation)
    return [(r.x + dx * c - dy * s, r.y + dx * s + dy * c)
            for dx, dy in ((-r.w / 2, -r.h / 2), (r.w / 2, -r.h / 2),
                           (r.w / 2, r.h / 2), (-r.w / 2, r.h / 2))]


def _flash_polys(obj) -> list:
    """A Flash/Region -> shapely polygons in BOARD space (y negated)."""
    out = []
    for prim in obj.to_primitives("mm"):
        if isinstance(prim, Rectangle):
            poly = Polygon([(x, -y) for x, y in _rect_pts(prim)]).buffer(0)
            if not poly.is_empty:
                out.append(poly)
            continue
        try:
            ap = prim.to_arc_poly()
            pts = list(ap.approximate_arcs(max_error=ARC_MAX_ERROR_MM).outline)
        except Exception:
            try:
                pts = list(prim.to_arc_poly().outline)
            except Exception:
                continue
        if len(pts) >= 3:
            poly = Polygon([(x, -y) for x, y in pts]).buffer(0)
            if not poly.is_empty:
                out.append(poly)
    if len(out) < 2:
        return out
    # One flash is one pad: a macro aperture (KiCad's RoundRect = a body
    # polygon + four corner circles + four edge lines) comes as several
    # primitives; kept apart, a check that takes "the pad around this hole"
    # measured the body alone, short by the corner radius on every side.
    u = unary_union(out)
    return [g for g in getattr(u, "geoms", [u]) if not g.is_empty]


def read_gerber(path: Path, name: str) -> LayerGeom:
    """Parse one gerber into board-space shapely geometry."""
    lg = LayerGeom(name=name)
    gf = GerberFile.open(str(path))
    for obj in gf.objects:
        if isinstance(obj, (Line, Arc)):
            o = obj.converted("mm")
            try:
                w = obj.aperture.equivalent_width("mm")
            except Exception:
                w = 0.0
            pts = [(o.x1, -o.y1), (o.x2, -o.y2)]
            if isinstance(o, Arc):
                # Never chord a drawn arc: the chord of a 3 mm rounded
                # Edge.Cuts corner runs ~0.88 mm inboard (false
                # copper_to_edge), and a KiCad fp_circle exports as two
                # 180-degree arcs whose chords BOTH collapse onto the
                # diameter (false silk_over_pad straight through the pad).
                try:
                    segs = o.approximate(max_error=ARC_MAX_ERROR_MM,
                                         unit="mm")
                except Exception:
                    segs = []
                if segs:
                    pts = ([(s.x1, -s.y1) for s in segs]
                           + [(segs[-1].x2, -segs[-1].y2)])
            ls = LineString(pts)
            if ls.length == 0:
                ls = LineString([(o.x1, -o.y1), (o.x1 + 1e-6, -o.y1)])
            lg.trace_lines.append((ls, w))
            lg.trace_widths.append(w)
            # ROUND caps: KiCad emits circular apertures (%ADDnC,...*%), so a
            # conformant CAM renders round trace ends. Flat caps truncate each
            # end by w/2, splitting genuinely overlapping same-net junctions
            # into phantom "islands" (S14: two false dfm_clearance errors on a
            # KiCad-clean board) and understating copper-to-edge proximity by
            # w/2 (false-negative direction).
            lg.traces.append(ls.buffer(max(w, 1e-6) / 2.0, cap_style=1,
                                       quad_segs=32))
        elif isinstance(obj, Flash):
            lg.pads.extend(_flash_polys(obj))
        elif isinstance(obj, Region):
            lg.pours.extend(_flash_polys(obj))
    return lg


_TA_PLATING = re.compile(r"#@!\s*TA\.AperFunction,(NonPlated|Plated)", re.I)
_TF_PLATING = re.compile(r"#@!\s*TF\.FileFunction,(NonPlated|Plated)", re.I)
_TOOL_DEF = re.compile(r"^T(\d+)C([\d.]+)", re.I)


def drill_plating(text: str) -> tuple[dict[float, bool], bool | None]:
    """KiCad's X2-style Excellon comments -> ({tool diameter mm: plated},
    file default). gerbonara leaves tool.plated None (and no tool number)
    for them, which read every NPTH (a USB-C peg, a mounting hole) as
    plated. A diameter that is both plated and not is left out (unknown)."""
    tools: dict[float, bool] = {}
    clash: set[float] = set()
    default = None
    pending = None
    for line in text.splitlines():
        line = line.strip()
        if m := _TF_PLATING.search(line):
            default = m.group(1).lower() == "plated"
        elif m := _TA_PLATING.search(line):
            pending = m.group(1).lower() == "plated"
        elif m := _TOOL_DEF.match(line):
            d = round(float(m.group(2)), 4)
            if pending is not None:
                if tools.get(d, pending) != pending:
                    clash.add(d)
                tools[d] = pending
            pending = None
        elif line in ("%", "M95"):
            break
    return {d: p for d, p in tools.items() if d not in clash}, default


def is_backdrill(path: Path) -> bool:
    """A KiCad back-drill file (#478): a controlled-depth pass over vias that
    are already in the through-hole file. It is no hole of its own, so hole
    spacing, plating and netlist reads must not see it."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "NonPlated,BackDrill" in line:
                    return True
                if line.strip() in ("%", "M95"):
                    break
    except OSError:
        pass
    return False


def read_drill(path: Path) -> list[Hole]:
    holes: list[Hole] = []
    try:
        tool_plated, file_plated = drill_plating(
            Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        tool_plated, file_plated = {}, None
    with warnings.catch_warnings():
        # kicad-cli emits G90 after the Excellon header; gerbonara warns but
        # parses it correctly. Suppressed here so every caller (gate.py runs
        # dfm_check as a library) gets clean stderr, not just the CLI.
        warnings.simplefilter("ignore")
        ef = ExcellonFile.open(str(path))
    for obj in ef.objects:
        if not isinstance(obj, Flash):
            continue
        o = obj.converted("mm")
        dia = getattr(getattr(obj, "tool", None), "diameter", None)
        if dia is None:
            dia = getattr(getattr(obj, "aperture", None), "diameter", None)
        if dia is None:
            continue
        plated = getattr(obj, "plated", None)
        if plated is None:
            tool = getattr(obj, "tool", None)
            plated = getattr(tool, "plated", None)
            if plated is None:
                plated = tool_plated.get(round(float(dia), 4))
            if plated is None:
                plated = file_plated
        holes.append(Hole(x=o.x, y=-o.y, diameter=float(dia),
                          plated=True if plated is None else bool(plated)))
    return holes


@dataclass
class Slot:
    """A routed drill hit (Excellon G85 / G00-G01 route): the hole is the
    line buffered by half the tool. KiCad writes an oval drill this way,
    even one a few microns off round (pinned core's USB-C J1: 0.3998 x
    0.4 mm, so a zero-length slot)."""
    line: LineString
    diameter: float
    plated: bool = True

    @property
    def poly(self):
        return self.line.buffer(max(self.diameter, 0.01) / 2.0, quad_segs=8)


def read_slots(path: Path) -> list[Slot]:
    """The routed hits read_drill leaves out (it keeps flashes only)."""
    try:
        tool_plated, file_plated = drill_plating(
            Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        tool_plated, file_plated = {}, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ef = ExcellonFile.open(str(path))
    out: list[Slot] = []
    for obj in ef.objects:
        if not isinstance(obj, (Line, Arc)):
            continue
        o = obj.converted("mm")
        tool = getattr(obj, "aperture", None) or getattr(obj, "tool", None)
        dia = getattr(tool, "diameter", None)
        if dia is None:
            continue
        plated = getattr(tool, "plated", None)
        if plated is None:
            plated = tool_plated.get(round(float(dia), 4))
        if plated is None:
            plated = file_plated
        a, b = (o.x1, -o.y1), (o.x2, -o.y2)
        line = LineString([a, b] if a != b else [a, (a[0] + 1e-6, a[1])])
        out.append(Slot(line=line, diameter=float(dia),
                        plated=True if plated is None else bool(plated)))
    return out


class FabStack:
    """Every gerber/drill file in one exported fab directory."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.copper_files: dict[str, Path] = {}
        self.silk_files: dict[str, Path] = {}
        self.mask_files: dict[str, Path] = {}
        self.paste_files: dict[str, Path] = {}
        self.edge_file: Path | None = None
        self.drill_files: list[Path] = []
        self.backdrill_files: list[Path] = []
        self._cache: dict[str, LayerGeom] = {}
        self._holes: list[Hole] | None = None
        self._slots: list[Slot] | None = None
        self._outline = None
        self._scan()

    def _scan(self) -> None:
        for p in sorted(self.dir.iterdir()):
            if not p.is_file():
                continue
            n = p.name
            if (m := _COPPER_RE.search(n)):
                self.copper_files[_layer_key(m.group(1))] = p
            elif (m := _SILK_RE.search(n)):
                self.silk_files[m.group(1)] = p
            elif (m := _MASK_RE.search(n)):
                self.mask_files[m.group(1)] = p
            elif (m := _PASTE_RE.search(n)):
                self.paste_files[m.group(1)] = p
            elif _EDGE_RE.search(n):
                self.edge_file = p
            elif _DRILL_RE.search(n) and not n.lower().endswith(".gbrjob"):
                (self.backdrill_files if is_backdrill(p)
                 else self.drill_files).append(p)
            elif (m := _COPPER_EXT_RE.search(n)):
                key = ("F.Cu" if m.group(1) else "B.Cu" if m.group(2)
                       else f"In{int(m.group(3))}.Cu")
                self.copper_files.setdefault(key, p)
            elif (m := _SIDE_EXT_RE.search(n)):
                side = "F" if m.group(1).lower() == "t" else "B"
                kind = {"o": self.silk_files, "s": self.mask_files,
                        "p": self.paste_files}[m.group(2).lower()]
                kind.setdefault(side, p)

    # ------------------------------------------------------------ accessors
    def _get(self, path: Path | None, name: str) -> LayerGeom | None:
        if path is None:
            return None
        if name not in self._cache:
            self._cache[name] = read_gerber(path, name)
        return self._cache[name]

    def copper_layer_names(self) -> list[str]:
        """Physical stackup order: F.Cu, In1.Cu, ..., B.Cu."""
        def key(n: str):
            if n == "F.Cu":
                return (0, 0)
            if n == "B.Cu":
                return (2, 0)
            m = re.match(r"In(\d+)\.Cu", n)
            return (1, int(m.group(1)) if m else 0)
        return sorted(self.copper_files, key=key)

    def copper(self, layer: str) -> LayerGeom | None:
        return self._get(self.copper_files.get(layer), layer)

    def silk(self, side: str) -> LayerGeom | None:
        return self._get(self.silk_files.get(side), f"{side}.Silkscreen")

    def mask(self, side: str) -> LayerGeom | None:
        return self._get(self.mask_files.get(side), f"{side}.Mask")

    def paste(self, side: str) -> LayerGeom | None:
        """Paste layer (stencil apertures). Flashes = pads that get paste."""
        return self._get(self.paste_files.get(side), f"{side}.Paste")

    @property
    def holes(self) -> list[Hole]:
        if self._holes is None:
            out: list[Hole] = []
            for p in self.drill_files:
                try:
                    out.extend(read_drill(p))
                except Exception:
                    continue
            self._holes = out
        return self._holes

    @property
    def slots(self) -> list[Slot]:
        """Routed drill hits (read_slots) from every drill file."""
        if self._slots is None:
            out: list[Slot] = []
            for p in self.drill_files:
                try:
                    out.extend(read_slots(p))
                except Exception:
                    continue
            self._slots = out
        return self._slots

    @property
    def outline(self):
        """Board outline polygon from Edge.Cuts centerlines (board space)."""
        if self._outline is None:
            self._outline = Polygon()
            if self.edge_file is not None:
                lg = self._get(self.edge_file, "Edge.Cuts")
                lines = [ls for ls, _ in lg.trace_lines]
                polys = []
                if lines:
                    snapped = set_precision(unary_union(_join_ends(lines)),
                                            OUTLINE_SNAP_MM)
                    polys = list(polygonize(snapped))
                if polys:
                    self._outline = max(polys, key=lambda p: p.area)
                elif lg.pours:
                    self._outline = unary_union(lg.pours)
        return self._outline


def open_fab(directory: Path | str) -> FabStack:
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"fab directory not found: {d}")
    return FabStack(d)
