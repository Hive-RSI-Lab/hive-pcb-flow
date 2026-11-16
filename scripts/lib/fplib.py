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
    return [float(x) for x in n[1:] if isinstance(x, (int, float))]


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

    @property
    def center(self) -> tuple[float, float]:
        return (self.at[0], self.at[1])

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
    return Pad(number=number, ptype=ptype, shape=shape,
               at=(an[0], an[1], rot), size=(sn[0], sn[1]),
               layers=[str(_tok(t)) for t in (_kid(node, "layers") or [None])[1:]],
               drill=max(dn) if dn else None,
               drill_xy=((dn[0], dn[1] if len(dn) > 1 else dn[0]) if dn else None),
               stroked_prims=_stroked_prims(node))


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


def unit_pins(sym_node) -> dict[int, list[tuple[str, float, float]]]:
    """{unit: [(pin number, x, y)]} of one top-level (symbol ...) node, from
    its NAME_<unit>_<body style> sub-symbols. Unit 0 holds the pins every
    unit shares (KiCad draws them on each); the De Morgan body (style 2)
    repeats style 1's pins and is skipped. A symbol with no sub-symbols
    (an `extends` alias) gives {}: resolve it to its parent first."""
    out: dict[int, list[tuple[str, float, float]]] = {}
    for sub in sym_node[1:] if _is_node(sym_node) else []:
        if not (_is_node(sub) and _head(sub) == "symbol" and len(sub) > 1):
            continue
        parts = str(_tok(sub[1])).rsplit("_", 2)
        if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        unit, style = int(parts[1]), int(parts[2])
        if style > 1:
            continue
        pins = out.setdefault(unit, [])
        for pin in sub[1:]:
            if not (_is_node(pin) and _head(pin) == "pin"):
                continue
            num, at = _kid(pin, "number"), _kid(pin, "at")
            xy = _nums(at)[:2] if at is not None else [0.0, 0.0]
            pins.append((str(_tok(num[1])) if num is not None and len(num) > 1
                         else "", *xy))
    return out


def symbol_units(kicad_sym_path: str | Path) -> dict[str, dict[int, list[str]]]:
    """{symbol name: {unit: [pin numbers]}} for every top-level symbol in a
    .kicad_sym library drawn as two or more units (an `extends` alias takes
    its parent's units). A board that places only unit 1 of one leaves the
    other units' pins out of the netlist (#378)."""
    root = _symbol_lib_root(kicad_sym_path)
    if root is None:
        return {}
    nodes, parent = {}, {}
    for child in root[1:]:
        if _is_node(child) and _head(child) == "symbol" and len(child) > 1:
            name = str(_tok(child[1]))
            nodes[name] = child
            ext = _kid(child, "extends")
            if ext is not None and len(ext) > 1:
                parent[name] = str(_tok(ext[1]))
    out = {}
    for name in nodes:
        base, hops = name, 0
        while base in parent and hops < 8:
            base, hops = parent[base], hops + 1
        units = {u: [p[0] for p in pins]
                 for u, pins in unit_pins(nodes.get(base)).items()}
        if len([u for u in units if u > 0]) > 1:
            out[name] = units
    return out
