"""fplib.py - parse a KiCad .kicad_mod footprint into pads + layer presence.

Handles BOTH footprint formats easyeda2kicad and KiCad emit:
  - legacy:  (module NAME (layer F.Cu) ... (pad 1 smd rect (at ..) (size ..)
             (layers F.Cu F.Paste F.Mask)))     <- unquoted layer tokens
  - modern:  (footprint "NAME" ... (pad "1" smd rect (at ..) (size ..)
             (layers "F.Cu" "F.Paste" "F.Mask")))

Pure sexpdata parse - no SWIG/KiCad process. Used by lib_pull.py (report) and
fp_verify.py (land-pattern diff). Everything is millimetres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import sexpdata


def _tok(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _is_node(n) -> bool:
    return isinstance(n, list) and bool(n)


def _head(n):
    return _tok(n[0]) if _is_node(n) else None


def _kid(n, name: str):
    for c in n[1:] if _is_node(n) else []:
        if _is_node(c) and _head(c) == name:
            return c
    return None


def _nums(n) -> list[float]:
    """The numbers of node `n` ([] for None)."""
    return [float(x) for x in n[1:] if isinstance(x, (int, float))] if n else []


def _layer_tokens(node) -> list[str]:
    """Layer name(s) of a graphic/pad node from its (layer X) or (layers ...)."""
    out = []
    for key in ("layer", "layers"):
        k = _kid(node, key)
        if k is not None:
            out += [str(_tok(t)) for t in k[1:]]
    return out


_COPPER_SUFFIX = ".Cu"
_GRAPHIC_HEADS = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc",
                  "fp_text", "fp_curve")


@dataclass
class Pad:
    number: str
    ptype: str          # smd | thru_hole | np_thru_hole | connect | ...
    shape: str          # rect | roundrect | oval | circle | custom | trapezoid
    at: tuple[float, float, float]   # x, y, rotation(deg)
    size: tuple[float, float]
    layers: list[str] = field(default_factory=list)
    drill: float | None = None       # largest drill dimension, mm (None for SMD)
    # drill (x, y) in the pad's own frame (a round drill is (d, d)); size and
    # drill both rotate with the pad, so axis i of one pairs with axis i of the other
    drill_xy: tuple[float, float] | None = None
    # custom-pad primitives that are closed shapes drawn stroked, not filled:
    # "gr_circle (width 0.3)". The Gerber plots the ring the stroke draws;
    # KiCad 10.0.6's DRC and connectivity saw only a stroked gr_circle's centre
    # line (#1133), and gr_poly / gr_rect are likely the same
    stroked_prims: list[str] = field(default_factory=list)
    # centre of the pad's copper bounding box, footprint frame: `at` for a
    # plain pad; a custom pad's primitives move it (a ring drawn round a port
    # off its anchor). None means `at`.
    copper_xy: tuple[float, float] | None = None
    # that bounding box's (w, h) along the footprint axes, rotation applied
    copper_wh: tuple[float, float] | None = None
    # (roundrect_rratio R): corner radius over the shorter side
    rratio: float = 0.25
    # a custom pad's anchor shape, (options (anchor rect|circle))
    anchor: str = "rect"
    # (drill ... (offset X Y)): the copper's offset from the hole (`at`) in
    # the pad's own frame; KiCad draws the shape there, the hole at `at`
    drill_offset: tuple[float, float] = (0.0, 0.0)

    @property
    def center(self) -> tuple[float, float]:
        return (self.at[0], self.at[1])

    @property
    def anchor_center(self) -> tuple[float, float]:
        """Where the pad's shape (a custom pad's anchor) is: `at` moved by
        the drill offset, turned with the pad."""
        ox, oy = self.drill_offset
        if not ox and not oy:
            return self.center
        r = math.radians(self.at[2])
        c, s = math.cos(r), math.sin(r)
        return (self.at[0] + ox * c + oy * s, self.at[1] - ox * s + oy * c)

    @property
    def anchor_size(self) -> tuple[float, float]:
        """That shape's bounding box (w, h) along the footprint's x and y
        (a custom pad's anchor only, without its primitives)."""
        shape = self.anchor if self.shape == "custom" else self.shape
        return _turned_wh(shape, self.size[0], self.size[1], self.at[2],
                          self.rratio)

    @property
    def copper_center(self) -> tuple[float, float]:
        """Where the pad's copper is: the centre of its bounding box."""
        return self.copper_xy if self.copper_xy is not None else self.anchor_center

    @property
    def copper_size(self) -> tuple[float, float]:
        """The copper bounding box (w, h) along the footprint's x and y."""
        return self.copper_wh if self.copper_wh is not None else self.anchor_size

    @property
    def is_copper(self) -> bool:
        # NPTH pads are mechanical (mounting holes) even if they list *.Cu.
        if self.ptype == "np_thru_hole":
            return False
        return any(ly.endswith(_COPPER_SUFFIX) or ly == "*.Cu" for ly in self.layers)


@dataclass
class Footprint:
    name: str
    path: Path
    pads: list[Pad]
    layers_present: set[str]          # every distinct layer token seen on graphics/pads

    @property
    def copper_pads(self) -> list[Pad]:
        return [p for p in self.pads if p.is_copper]

    def has_layer_kind(self, kind: str) -> bool:
        """True if any graphic sits on a *.<kind> layer (CrtYd, SilkS, Fab)."""
        return any(kind in ly for ly in self.layers_present)

    @property
    def has_courtyard(self) -> bool:
        return self.has_layer_kind("CrtYd")


_CLOSED_PRIMS = ("gr_circle", "gr_poly", "gr_rect")


def _stroked_prims(node) -> list[str]:
    """Closed primitives of a custom pad drawn with a stroke and no fill, as
    "gr_circle (width 0.3)": width > 0 and (fill no|none), or a gr_circle with
    no fill token (a ring in the KiCad 5 format too). A gr_poly or gr_rect with
    no fill token is filled: KiCad fills those in old-format files, which is
    what easyeda2kicad writes ("(gr_poly (pts ...) (width 0.1))")."""
    prims = _kid(node, "primitives")
    out = []
    for n in (prims[1:] if prims is not None else []):
        if not _is_node(n) or _head(n) not in _CLOSED_PRIMS:
            continue
        wn = _kid(n, "width")
        if wn is None and _kid(n, "stroke") is not None:
            wn = _kid(_kid(n, "stroke"), "width")
        w = (_nums(wn) or [0.0])[0] if wn is not None else 0.0
        fill = _kid(n, "fill")
        if fill is None:
            unfilled = _head(n) == "gr_circle"
        else:
            unfilled = not any(str(_tok(x)) in ("yes", "solid", "true") for x in fill[1:])
        if w > 0 and unfilled:
            out.append(f"{_head(n)} (width {w:g})")
    return out


def _parse_pad(node) -> Pad | None:
    # (pad NUMBER TYPE SHAPE ...)
    if len(node) < 4:
        return None
    number = str(_tok(node[1]))
    ptype = str(_tok(node[2]))
    shape = str(_tok(node[3]))
    at = _kid(node, "at")
    size = _kid(node, "size")
    if at is None or size is None:
        return None
    an = _nums(at)
    sn = _nums(size)
    if len(an) < 2 or len(sn) < 2:
        return None
    rot = an[2] if len(an) >= 3 else 0.0
    # (drill D) or (drill oval W H): `drill` keeps the LARGEST dimension;
    # fp_verify's annulus uses `drill_xy` per axis.
    drill = _kid(node, "drill")
    dn = _nums(drill) if drill is not None else []
    off = _xy(_kid(drill, "offset")) if drill is not None else None
    off = off or (0.0, 0.0)
    rr = _nums(_kid(node, "roundrect_rratio")) or [0.25]
    anchor = _kid(_kid(node, "options"), "anchor")
    copper_xy = copper_wh = None
    box = _custom_bbox(node, sn[0], sn[1]) if shape == "custom" else None
    if box is not None:
        x0, y0, x1, y1 = box
        copper_xy, copper_wh = _place_bbox(
            (x0 + off[0], y0 + off[1], x1 + off[0], y1 + off[1]), an[0], an[1], rot)
    return Pad(number=number, ptype=ptype, shape=shape,
               at=(an[0], an[1], rot), size=(sn[0], sn[1]),
               layers=[str(_tok(t)) for t in (_kid(node, "layers") or [None])[1:]],
               drill=max(dn) if dn else None,
               drill_xy=((dn[0], dn[1] if len(dn) > 1 else dn[0]) if dn else None),
               stroked_prims=_stroked_prims(node),
               copper_xy=copper_xy, copper_wh=copper_wh, rratio=rr[0],
               anchor=str(_tok(anchor[1])) if anchor is not None and len(anchor) > 1
               else "rect", drill_offset=off)


def _xy(node) -> tuple[float, float] | None:
    n = _nums(node) if node is not None else []
    return (n[0], n[1]) if len(n) >= 2 else None


def _turned_wh(shape: str, w: float, h: float, rot: float,
               rratio: float = 0.25) -> tuple[float, float]:
    """The axis-aligned (w, h) of a w x h pad shape turned `rot` degrees:
    exact for circle, oval (a stadium), rect and roundrect; any other shape
    (trapezoid, chamfered) by its rectangle."""
    r = math.radians(rot)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    if shape == "circle":
        return (w, w)
    if shape == "oval":
        a = abs(w - h) / 2.0              # half the straight part
        if w >= h:
            return (2 * a * c + h, 2 * a * s + h)
        return (2 * a * s + w, 2 * a * c + w)
    if shape == "roundrect":
        rad = min(max(rratio, 0.0), 0.5) * min(w, h)
        iw, ih = w - 2 * rad, h - 2 * rad
        return (iw * c + ih * s + 2 * rad, iw * s + ih * c + 2 * rad)
    return (w * c + h * s, w * s + h * c)


def _arc_extent(a, m, b) -> list[tuple[float, float]]:
    """Points bounding the arc a -> m -> b: its ends plus every axis extreme
    the sweep passes (the circle's left/right/top/bottom)."""
    (x1, y1), (x2, y2), (x3, y3) = a, m, b
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [a, m, b]
    cx = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1)
          + (x3**2 + y3**2) * (y1 - y2)) / d
    cy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3)
          + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - cx, y1 - cy)
    t1, t2, t3 = (math.atan2(y - cy, x - cx) for x, y in (a, m, b))
    tau = 2 * math.pi

    def sweep(frm, to):                   # angle from `frm` to `to`, increasing
        return (to - frm) % tau

    span = sweep(t1, t3)
    if sweep(t1, t2) > span:              # the mid point is on the other way
        t1, span = t3, tau - span
    pts = [a, b]
    for k in range(4):
        t = k * math.pi / 2
        if sweep(t1, t) <= span:
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


def _custom_bbox(node, w: float, h: float):
    """A custom pad's copper bounding box (x0, y0, x1, y1) in its own frame:
    the anchor (`size`, centred on the pad origin) and every gr_* primitive,
    strokes included. None when the pad has no primitives."""
    prims = _kid(node, "primitives")
    if prims is None:
        return None
    pts: list[tuple[float, float, float]] = [(-w / 2, -h / 2, 0.0), (w / 2, h / 2, 0.0)]
    for n in prims[1:]:
        if not _is_node(n):
            continue
        kind = _head(n)
        wn = _kid(n, "width")
        if wn is None and _kid(n, "stroke") is not None:
            wn = _kid(_kid(n, "stroke"), "width")
        half = (_nums(wn)[0] if wn is not None and _nums(wn) else 0.0) / 2.0
        if kind == "gr_circle":
            c, e = _xy(_kid(n, "center")), _xy(_kid(n, "end"))
            if c and e:
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                pts += [(c[0] - r, c[1] - r, half), (c[0] + r, c[1] + r, half)]
        elif kind in ("gr_line", "gr_rect"):
            for k in ("start", "end"):
                q = _xy(_kid(n, k))
                if q:
                    pts.append((q[0], q[1], half))
        elif kind == "gr_arc":
            a, m, b = (_xy(_kid(n, k)) for k in ("start", "mid", "end"))
            ang = _nums(_kid(n, "angle"))
            if a and b and not m and ang:
                # KiCad 5: (start) is the centre, (end) where the arc
                # begins, (angle A) its sweep, clockwise on screen
                c, a = a, b
                vx, vy = a[0] - c[0], a[1] - c[1]

                def turn(t):                  # KiCad's RotatePoint by t deg
                    t = math.radians(t)
                    return (c[0] + vx * math.cos(t) + vy * math.sin(t),
                            c[1] + vy * math.cos(t) - vx * math.sin(t))
                m, b = turn(-ang[0] / 2), turn(-ang[0])
            if a and m and b:
                pts += [(x, y, half) for x, y in _arc_extent(a, m, b)]
        elif kind in ("gr_poly", "gr_curve"):
            ptsn = _kid(n, "pts")
            for q in (ptsn[1:] if ptsn is not None else []):
                if not _is_node(q):
                    continue
                if _head(q) == "xy" and _xy(q):
                    pts.append((*_xy(q), half))
                elif _head(q) == "arc":
                    a, m, b = (_xy(_kid(q, k)) for k in ("start", "mid", "end"))
                    if a and m and b:
                        pts += [(x, y, half) for x, y in _arc_extent(a, m, b)]
    return (min(x - e for x, _, e in pts), min(y - e for _, y, e in pts),
            max(x + e for x, _, e in pts), max(y + e for _, y, e in pts))


def _place_bbox(box, ax: float, ay: float, rot: float):
    """A pad-frame box placed in the footprint: the centre turned by the pad
    angle (KiCad: positive turns counter-clockwise on screen, y down) and the
    turned box's axis-aligned (w, h)."""
    x0, y0, x1, y1 = box
    px, py = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    r = math.radians(rot)
    c, s = math.cos(r), math.sin(r)
    w, h = x1 - x0, y1 - y0
    centre = (ax + px * c + py * s, ay - px * s + py * c)
    return centre, (w * abs(c) + h * abs(s), w * abs(s) + h * abs(c))


def _strip_lib_prefix(name: str) -> str:
    # "easyeda2kicad:C0402" -> "C0402"
    return name.split(":")[-1]


def parse_footprint(path: str | Path) -> Footprint:
    """Parse one .kicad_mod file into a Footprint."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    root = sexpdata.loads(text)
    if not _is_node(root) or _head(root) not in ("module", "footprint"):
        raise ValueError(f"{p} is not a footprint (head={_head(root)})")
    name = _strip_lib_prefix(str(_tok(root[1]))) if len(root) > 1 else p.stem

    pads: list[Pad] = []
    layers: set[str] = set()
    for child in root[1:]:
        if not _is_node(child):
            continue
        h = _head(child)
        if h == "pad":
            pad = _parse_pad(child)
            if pad is not None:
                pads.append(pad)
                layers.update(pad.layers)
        elif h in _GRAPHIC_HEADS:
            layers.update(_layer_tokens(child))
    return Footprint(name=name, path=p, pads=pads, layers_present=layers)


def footprint_files(pretty_dir: str | Path) -> list[Path]:
    d = Path(pretty_dir)
    return sorted(d.glob("*.kicad_mod")) if d.is_dir() else []


def _symbol_lib_root(kicad_sym_path: str | Path):
    p = Path(kicad_sym_path)
    if not p.exists():
        return None
    try:
        root = sexpdata.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if not _is_node(root) or _head(root) != "kicad_symbol_lib":
        return None
    return root


def symbol_names(kicad_sym_path: str | Path) -> list[str]:
    """Top-level symbol names in a .kicad_sym library (best-effort)."""
    root = _symbol_lib_root(kicad_sym_path)
    if root is None:
        return []
    names = []
    for child in root[1:]:
        if _is_node(child) and _head(child) == "symbol" and len(child) > 1:
            names.append(str(_tok(child[1])))
    return names


def symbol_index(kicad_sym_path: str | Path) -> list[dict]:
    """Top-level symbols with their 'LCSC Part', 'Footprint' and 'Reference'
    properties.

    The per-part presence ground truth (LEARNINGS 2026-07-28 [easyeda2kicad]
    [parts]): a part is present iff the symbol lib holds a top-level
    (symbol ...) block carrying (property "LCSC Part" "<id>") whose Footprint
    property resolves to a file in the .pretty dir. Footprint files are SHARED
    across parts and record only the FIRST puller's id, so never map
    LCSC -> footprint by grepping .kicad_mod text (C2580 also substring-matches
    C25804's file). Reads utf-8 with errors='replace': pulled libs carry
    CJK/Ohm characters.
    """
    root = _symbol_lib_root(kicad_sym_path)
    if root is None:
        return []
    out = []
    for child in root[1:]:
        if not (_is_node(child) and _head(child) == "symbol" and len(child) > 1):
            continue
        props: dict[str, str] = {}
        for c in child[1:]:
            if _is_node(c) and _head(c) == "property" and len(c) >= 3:
                props[str(_tok(c[1]))] = str(_tok(c[2]))
        out.append({"name": str(_tok(child[1])),
                    "lcsc": props.get("LCSC Part", ""),
                    "footprint": props.get("Footprint", ""),
                    "reference": props.get("Reference", "")})
    return out
