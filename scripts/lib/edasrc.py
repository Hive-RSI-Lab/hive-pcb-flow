"""edasrc.py - what a board from another EDA tool holds, and what KiCad kept.

eda_io import runs KiCad's own importer (kicad-cli pcb import). The importer
reports success even when it drops a copper layer, and its JSON report says
`warnings: []` while stderr lists the layers it skipped. Here hwde counts the
SOURCE file itself (parts, nets, objects per copper layer), summarizes the
imported .kicad_pcb the same way, and compares the two, so a loss is said in
numbers ("Int2 (GND) had 220 copper objects; the import has no such layer")
instead of trusted to the importer.

Readers, one per format KiCad 10 imports headless (all return a Source):

  altium     .PcbDoc / .CSPcbDoc (OLE compound file, lib/cfb.py)
  eagle      .brd (Eagle 6+ XML; Fusion Electronics' EAGLE export)
  easyeda    EasyEDA / JLCEDA Std .json
  fabmaster  Allegro / OrCAD PCB Editor extract .txt (Fabmaster "A!" format)
  pads       PADS ASCII .asc (parts and nets; copper isn't counted per layer)

`detect(path)` names the format from the file's own bytes, not its
extension, and says plainly when it's a format nothing here can read
(Allegro's binary .brd, EasyEDA Pro, OrCAD Capture).

Pure python, no KiCad: tests feed it synthetic files.
"""
from __future__ import annotations

import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import cfb


@dataclass
class Source:
    format: str
    refs: set[str] = field(default_factory=set)
    nets: set[str] = field(default_factory=set)
    # copper layer name (as the source names it) -> objects on it (tracks,
    # arcs, pours, regions); None when the reader can't count per layer
    copper: dict[str, int] | None = field(default_factory=dict)
    # the source's copper layer count as it declares it, when it does
    copper_declared: int | None = None
    vias: int | None = None
    # non-copper layer name -> drawings on it, when the reader counts them
    other: dict[str, int] = field(default_factory=dict)
    # source copper layer name -> the KiCad layer the importer puts it on,
    # for importers whose report names KiCad's layers, not the source's
    kicad_names: dict[str, str] = field(default_factory=dict)
    # Eagle: Milling-layer (46) wires in a package that run through a drilled
    # pad, i.e. a slotted hole KiCad's importer drops (it puts them on a layer
    # named UNDEFINED). Package coordinates, Eagle's y-up:
    # {"ref", "pad", "x1", "y1", "x2", "y2", "w"} in mm
    slots: list[dict] = field(default_factory=list)
    # Milling wires that aren't a pad's slot (board cut-outs, or in a package
    # with no pad under them): counted, not converted
    milling_other: int = 0
    # Eagle: the board's design rules (<designrules> params, mm) and net
    # classes [{name, width, drill, clearance, nets}] - KiCad's importer
    # carries neither, so the board comes in at KiCad's defaults
    rules: dict = field(default_factory=dict)
    classes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Altium: schematic designator (SOURCEDESIGNATOR) -> the designator the
    # PCB shows, where they differ (PCB-side renames never annotated back).
    # KiCad's importer takes the PCB's.
    pcb_refs: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"format": self.format, "parts": len(self.refs),
                "nets": len(self.nets), "copper": self.copper,
                "copper_declared": self.copper_declared, "vias": self.vias,
                "notes": self.notes, "pcb_refs": self.pcb_refs}


class Unreadable(ValueError):
    """A file nothing here can import, with what to do instead."""


# --------------------------------------------------------------- detection

def detect(path: Path) -> str:
    """altium | eagle | easyeda | easyedapro | fabmaster | pads | cadstar |
    pcad | kicad | allegro-brd | orcad-capture | unknown."""
    head = path.read_bytes()[:4096]
    suf = path.suffix.lower()
    if head[:8] == cfb.MAGIC:
        if suf in (".dsn", ".olb", ".opj"):
            return "orcad-capture"
        return "altium"
    if head[:2] == b"PK":
        try:
            names = zipfile.ZipFile(path).namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if suf in (".epro", ".epro2") or any(n.endswith(".epru") or n == "project.json" for n in names):
            return "easyedapro"
        return "unknown"
    text = head.decode("latin1")
    s = text.lstrip()
    if s.startswith("(kicad_pcb"):
        return "kicad"
    if s.startswith("<?xml") or s.startswith("<!DOCTYPE eagle") or s.startswith("<eagle"):
        return "eagle" if "<eagle" in text or "eagle.dtd" in text else "unknown"
    if s.startswith("{") and '"docType"' in text or (s.startswith("{") and '"shape"' in text):
        return "easyeda"
    if s.startswith("A!") or "\nA!" in text[:512]:
        return "fabmaster"
    if s.startswith("!PADS-") or s.startswith("*PADS-"):
        return "pads"
    if s.startswith("(CADSTARPCB") or s.startswith("CADSTARPCB"):
        return "cadstar"
    if s.startswith("(PCB_FILE") or s.startswith("ACCEL_ASCII"):
        return "pcad"
    if suf == ".brd" and b"\x00" in head:
        return "allegro-brd"
    return "unknown"


# What to do when the file can't come in headless. Plain, one entry each.
NOT_IMPORTABLE = {
    "allegro-brd": "Allegro/OrCAD PCB Editor's binary .brd is closed; KiCad 10 "
                   "reads Allegro boards only as a Fabmaster extract. In Allegro "
                   "run File > Export > Extracts (or the `extracta` command) with "
                   "the views KiCad's Fabmaster importer documents, and import "
                   "the .txt it writes.",
    "easyedapro": "KiCad 10.0.6's EasyEDA Pro importer crashes headless "
                  "(bad_function_call: it asks which board of the project to "
                  "import, and kicad-cli has no one to ask). In EasyEDA Pro use "
                  "Export > Altium Designer (or the KiCad export) and import that "
                  "file, or import the .epro in the KiCad GUI (File > Import > "
                  "Non-KiCad Board File).",
    "orcad-capture": "OrCAD Capture schematics (.dsn/.olb) have no importer in "
                     "KiCad. Bring the board instead (Allegro extract), and for "
                     "the netlist export an Allegro/Telesis netlist from Capture.",
}


# ------------------------------------------------------------------ altium

# Altium's legacy layer ids: 1 top, 2..31 mid 1..30, 32 bottom,
# 39..54 internal plane 1..16, 74 multi-layer.
def _altium_copper(lid: int) -> bool:
    return 1 <= lid <= 32 or 39 <= lid <= 54


def _altium_poly_layer(name: str) -> int | None:
    name = name.upper()
    if name == "TOP":
        return 1
    if name == "BOTTOM":
        return 32
    m = re.fullmatch(r"MID(\d+)", name)
    if m:
        return 1 + int(m.group(1))
    m = re.fullmatch(r"PLANE(\d+)", name)
    if m:
        return 38 + int(m.group(1))
    return None


def _props(blob: bytes) -> dict[str, str]:
    txt = blob.decode("latin1")
    return dict(kv.split("=", 1) for kv in txt.split("|") if "=" in kv)


def _length_prefixed(blob: bytes):
    """Records of a text stream: uint32 length + |KEY=VAL|... text."""
    i = 0
    while i + 4 <= len(blob):
        n = struct.unpack_from("<I", blob, i)[0] & 0x00FFFFFF
        yield blob[i + 4:i + 4 + n]
        i += 4 + n


def _typed_records(blob: bytes):
    """Records of a binary primitive stream: uint8 type, uint32 len, body."""
    i = 0
    while i + 5 <= len(blob):
        t = blob[i]
        n = struct.unpack_from("<I", blob, i + 1)[0]
        yield t, blob[i + 5:i + 5 + n]
        i += 5 + n


def _altium_designator_texts(blob: bytes) -> dict[int, str]:
    """Texts6: component index -> its designator text as the PCB shows it
    (what KiCad's importer takes as the reference). Each record is type 5,
    then a property block (layer u8, ..., component u16 at 7, ..., is_comment
    u8 at 40, is_designator u8 at 41) and a Pascal string block."""
    out: dict[int, str] = {}
    i = 0
    while i + 5 <= len(blob):
        i += 1
        n1 = struct.unpack_from("<I", blob, i)[0]
        b1 = blob[i + 4:i + 4 + n1]
        i += 4 + n1
        if i + 4 > len(blob):
            break
        n2 = struct.unpack_from("<I", blob, i)[0]
        b2 = blob[i + 4:i + 4 + n2]
        i += 4 + n2
        if len(b1) > 41 and b1[41] and b2:
            comp = struct.unpack_from("<H", b1, 7)[0]
            if comp != 0xFFFF:
                out[comp] = b2[1:1 + b2[0]].decode("latin1")
    return out


def read_altium(path: Path) -> Source:
    files = cfb.read(path.read_bytes())
    src = Source("altium")
    board = _props(files.get("Board6/Data", b"")[4:])
    names = {int(m.group(1)): v for k, v in board.items()
             if (m := re.fullmatch(r"LAYER(\d+)NAME", k))}
    # the stack: LAYER1 -> NEXT -> ... -> 32
    stack, lid = [], 1
    while lid and lid not in stack and len(stack) < 64:
        stack.append(lid)
        lid = int(board.get(f"LAYER{lid}NEXT", "0") or 0)
    stack = [x for x in stack if _altium_copper(x)]
    src.copper_declared = len(stack) or None
    counts: Counter[int] = Counter()
    for stream in ("Tracks6/Data", "Arcs6/Data", "Fills6/Data", "Regions6/Data"):
        for _t, body in _typed_records(files.get(stream, b"")):
            if body and _altium_copper(body[0]):
                counts[body[0]] += 1
    for rec in _length_prefixed(files.get("Polygons6/Data", b"")):
        lay = _altium_poly_layer(_props(rec).get("LAYER", ""))
        if lay:
            counts[lay] += 1
    src.vias = sum(1 for _ in _typed_records(files.get("Vias6/Data", b"")))
    for lid in sorted(set(counts) | set(stack) | {x for x in names if _altium_copper(x)}):
        name = names.get(lid) or f"layer {lid}"
        src.copper[name] = counts.get(lid, 0)
        if lid not in stack and counts.get(lid):
            src.notes.append(f"{name} holds {counts[lid]} copper objects but "
                             "isn't in Altium's layer stack (a disabled "
                             "layer: Altium doesn't fabricate it either)")
    shown = _altium_designator_texts(files.get("Texts6/Data", b""))
    for i, rec in enumerate(_length_prefixed(files.get("Components6/Data", b""))):
        ref = _props(rec).get("SOURCEDESIGNATOR")
        if ref:
            src.refs.add(ref)
            if shown.get(i) and shown[i] != ref:
                src.pcb_refs[ref] = shown[i]
    for rec in _length_prefixed(files.get("Nets6/Data", b"")):
        name = _props(rec).get("NAME")
        if name:
            src.nets.add(name)
    return src


# ------------------------------------------------------------------- eagle

def read_eagle(path: Path) -> Source:
    root = ET.parse(path).getroot()
    src = Source("eagle")
    names = {int(l.get("number")): l.get("name") for l in root.iter("layer")}
    counts: Counter[int] = Counter()
    board = root.find(".//board")
    if board is None:
        raise Unreadable("an Eagle file with no <board>: a schematic or library?")
    # board copper is under <plain> and <signals>; the parts' own pads and
    # graphics sit in <libraries> and come in with the parts
    for sec in ("plain", "signals"):
        node = board.find(sec)
        for tag in ("wire", "polygon", "rectangle", "circle"):
            for el in (node.iter(tag) if node is not None else ()):
                lay = int(el.get("layer", "0"))
                if 1 <= lay <= 16:
                    counts[lay] += 1
    for lay, n in sorted(counts.items()):
        src.copper[names.get(lay, f"layer {lay}")] = n
    # KiCad's Eagle importer reports KiCad's names: Top/Bottom are F.Cu/B.Cu,
    # and the active inner layers (Route2..15) become In1, In2, ... in number
    # order, packed: a 4-layer board on Eagle layers 2 and 15 gets In1/In2
    active = {int(l.get("number")) for l in root.iter("layer")
              if l.get("active", "yes") != "no"}
    inner = 0
    for lay in range(1, 17):
        name = names.get(lay)
        if not name or not (lay in active or counts.get(lay)):
            continue
        if 1 < lay < 16:
            inner += 1
        src.kicad_names[name] = "F.Cu" if lay == 1 else "B.Cu" if lay == 16 else f"In{inner}.Cu"
    src.vias = sum(1 for _ in board.iter("via"))
    _eagle_slots(root, board, src)
    _eagle_rules(root, board, src)
    src.refs = {e.get("name") for e in board.iter("element") if e.get("name")}
    src.nets = {eagle_overbar(s.get("name")) for s in board.iter("signal") if s.get("name")}
    return src


def _eagle_slots(root, board, src: Source) -> None:
    """Milling (46) wires through a package's drilled pads: slots."""
    pkgs = {}
    for lib in root.iter("library"):
        for pk in lib.iter("package"):
            pkgs[(lib.get("name"), lib.get("urn"), pk.get("name"))] = pk
    plain = board.find("plain")
    src.milling_other += sum(1 for w in (plain.iter("wire") if plain is not None else ())
                             if w.get("layer") == "46")
    for el in board.iter("element"):
        pk = pkgs.get((el.get("library"), el.get("library_urn"), el.get("package")))
        if pk is None:
            pk = next((v for (l, _, n), v in pkgs.items()
                       if (l, n) == (el.get("library"), el.get("package"))), None)
        if pk is None:
            continue
        pads = [(p.get("name"), float(p.get("x")), float(p.get("y")), float(p.get("drill", 0)))
                for p in pk.iter("pad") if p.get("drill")]
        for w in pk.iter("wire"):
            if w.get("layer") != "46":
                continue
            x1, y1, x2, y2 = (float(w.get(k)) for k in ("x1", "y1", "x2", "y2"))
            width = float(w.get("width", 0))
            hit = None
            for name, px, py, drill in pads:
                dx, dy = x2 - x1, y2 - y1
                t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
                if math.hypot(x1 + t * dx - px, y1 + t * dy - py) <= (width + drill) / 2:
                    hit = name
                    break
            if hit:
                src.slots.append({"ref": el.get("name"), "pad": hit, "x1": x1, "y1": y1,
                                  "x2": x2, "y2": y2, "w": width})
            else:
                src.milling_other += 1


def _eagle_len(v: str | None) -> float | None:
    """An Eagle length ('4mil', '0.2mm', '0.01in', '0.25' ratio) in mm."""
    if not v:
        return None
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*(mil|mm|in|mic)?\s*", v)
    if not m:
        return None
    n = float(m.group(1))
    return {"mil": n * 0.0254, "mm": n, "in": n * 25.4, "mic": n / 1000,
            None: n}[m.group(2)]


def _eagle_rules(root, board, src: Source) -> None:
    """<designrules> params and <classes> of an Eagle board, in mm."""
    p = {e.get("name"): e.get("value") for e in board.iter("param")}
    if not p:
        return
    mm = {k: _eagle_len(v) for k, v in p.items()}
    dist = [mm.get(k) for k in ("mdWireWire", "mdWirePad", "mdWireVia", "mdPadPad", "mdPadVia",
                                "mdViaVia", "mdSmdPad", "mdSmdVia", "mdSmdSmd")]
    dist = [d for d in dist if d]
    drill = mm.get("msDrill")
    ring = mm.get("rlMinViaOuter")
    # Eagle's via ring: rvViaOuter x drill, clamped to [rlMinViaOuter, rlMaxViaOuter]
    if drill and ring is not None:
        rv, rmax = mm.get("rvViaOuter") or 0, mm.get("rlMaxViaOuter") or ring
        ring = min(max(rv * drill, ring), rmax)
    src.rules = {k: round(v, 4) for k, v in {
        "clearance": min(dist) if dist else None,
        "track_width": mm.get("msWidth"),
        "via_drill": drill,
        "via_annular": ring,
        "via_diameter": drill + 2 * ring if drill and ring is not None else None,
        "copper_edge": mm.get("mdCopperDimension"),
        "hole_clearance": mm.get("mdDrill"),
    }.items() if v}
    members: dict[str, list[str]] = {}
    for sig in board.iter("signal"):
        members.setdefault(sig.get("class", "0"), []).append(eagle_overbar(sig.get("name")))
    for c in board.iter("class"):
        num = c.get("number")
        clr = [_eagle_len(x.get("value")) for x in c.iter("clearance") if x.get("class") == num]
        src.classes.append({"number": num, "name": c.get("name"),
                            "width": _eagle_len(c.get("width")) or 0.0,
                            "drill": _eagle_len(c.get("drill")) or 0.0,
                            "clearance": (clr[0] if clr else 0.0) or 0.0,
                            "nets": sorted(members.get(num, []))})


def eagle_overbar(name: str) -> str:
    """An Eagle name as KiCad's importer writes it: Eagle's '!' toggles an
    overbar ('!RESET!', 'SPI_!CS!'; an unclosed '!WR' runs to the end) and
    KiCad writes it '~{..}'. A '!' with nothing after it is dropped."""
    out, i, bar = [], 0, False
    while i < len(name):
        c = name[i]
        if c == "!":
            if bar:
                out.append("}")
            elif i + 1 < len(name):
                out.append("~{")
            else:
                i += 1
                continue
            bar = not bar
        else:
            out.append(c)
        i += 1
    if bar:
        out.append("}")
    return "".join(out)


# ----------------------------------------------------------------- easyeda

_EE_COPPER = {1: "TopLayer", 2: "BottomLayer"} | {20 + i: f"Inner{i}" for i in range(1, 33)}


def read_easyeda(path: Path) -> Source:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if str(doc.get("head", {}).get("docType")) not in ("3", ""):
        raise Unreadable("an EasyEDA Std document that isn't a PCB "
                         f"(docType {doc.get('head', {}).get('docType')})")
    src = Source("easyeda")
    names = dict(_EE_COPPER)
    active = 0
    for row in doc.get("layers", []):
        f = row.split("~")
        if len(f) > 3 and f[0].isdigit() and int(f[0]) in _EE_COPPER:
            names[int(f[0])] = f[1]
            if f[3] == "true":
                active += 1
    src.copper_declared = active or None
    counts: Counter[int] = Counter()
    vias = 0

    def net(v: str) -> None:
        if v:
            src.nets.add(v)

    for shape in doc.get("shape", []):
        f = shape.split("~")
        kind = f[0]
        try:
            if kind in ("TRACK", "ARC", "COPPERAREA") and f[2].isdigit():
                lay = int(f[2])
                if lay in names:
                    counts[lay] += 1
                net(f[3])
            elif kind == "SOLIDREGION" and f[1].isdigit():
                if int(f[1]) in names and (len(f) < 5 or f[4] == "solid"):
                    counts[int(f[1])] += 1
                net(f[2])
            elif kind == "VIA":
                vias += 1
                net(f[4])
            elif kind == "PAD":
                net(f[7])
            elif kind == "LIB":
                for part in shape.split("#@$"):
                    p = part.split("~")
                    if p[0] == "TEXT" and len(p) > 10 and p[1] == "P" and p[10]:
                        src.refs.add(p[10])
                    elif p[0] == "PAD" and len(p) > 7:
                        net(p[7])
        except IndexError:
            continue
    for lay, n in sorted(counts.items()):
        src.copper[names[lay]] = n
    src.vias = vias
    return src


# --------------------------------------------------------------- fabmaster

def _fab_sections(path: Path):
    """Yield (header columns, row) for every S! row under its A! header."""
    cols: list[str] = []
    with path.open(encoding="latin1") as fh:
        for line in fh:
            if line.startswith("A!"):
                cols = line.rstrip("\n").rstrip("!").split("!")[1:]
            elif line.startswith("S!") and cols:
                yield cols, line.rstrip("\n").split("!")[1:]


def read_fabmaster(path: Path) -> Source:
    src = Source("fabmaster")
    counts: Counter[str] = Counter()
    for cols, row in _fab_sections(path):
        r = dict(zip(cols, row))
        if cols[:2] == ["REFDES", "COMP_CLASS"] and r.get("REFDES"):
            src.refs.add(r["REFDES"])
        elif cols[:1] == ["NET_NAME"] and r.get("NET_NAME"):
            src.nets.add(r["NET_NAME"])
        elif cols[:2] == ["CLASS", "SUBCLASS"] and len(cols) > 2:
            sub = r.get("SUBCLASS", "")
            if r.get("CLASS") == "ETCH":
                counts[sub] += 1
            elif sub:
                src.other[sub] = src.other.get(sub, 0) + 1
        elif cols[:2] == ["VIA_X", "VIA_Y"]:
            src.vias = (src.vias or 0) + 1
    src.copper = dict(sorted(counts.items()))
    return src


# -------------------------------------------------------------------- pads

_PADS_PART = re.compile(r"^(\S+)\s+(\S+)\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?[\d.]+\s+[UG]\s+[NM]\b")


def read_pads(path: Path) -> Source:
    src = Source("pads", copper=None)
    section = ""
    for line in path.read_text(encoding="latin1").splitlines():
        if line.startswith("*") and not line.startswith("*REMARK*"):
            section = line.split("*")[1]
            if section == "SIGNAL":
                parts = line.split()
                if len(parts) > 1:
                    src.nets.add(parts[1])
            continue
        if section == "PCB" and line.startswith("MAXIMUMLAYER"):
            try:
                src.copper_declared = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif section == "PART":
            m = _PADS_PART.match(line)
            if m:
                src.refs.add(m.group(1))
    src.notes.append("PADS copper isn't counted per layer here: only parts, "
                     "nets and the declared layer count are compared")
    return src


READERS = {"altium": read_altium, "eagle": read_eagle,
           "easyeda": read_easyeda, "fabmaster": read_fabmaster,
           "pads": read_pads}


def read_source(path: Path, fmt: str | None = None) -> Source | None:
    """The source's counts, or None for a format with no reader here
    (CADSTAR, P-CAD: imported, but not counted)."""
    fmt = fmt or detect(path)
    if fmt in NOT_IMPORTABLE:
        raise Unreadable(NOT_IMPORTABLE[fmt])
    reader = READERS.get(fmt)
    return reader(path) if reader else None


# ---------------------------------------------------------- the KiCad side

@dataclass
class Board:
    copper_layers: list[str] = field(default_factory=list)   # F.Cu, In1.Cu, ...
    layer_names: dict[str, str] = field(default_factory=dict)  # In1.Cu -> user name
    copper: Counter = field(default_factory=Counter)  # layer -> tracks/arcs/zones
    refs: list[str] = field(default_factory=list)
    # footprints with (attr ... board_only): silk art, logos, a frame - no
    # component, so no ODB++/IPC-2581 component record either
    board_only: set[str] = field(default_factory=set)
    nets: set[str] = field(default_factory=set)
    vias: int = 0
    zones: int = 0
    zones_unfilled: int = 0
    footprints: int = 0
    footprints_bottom: int = 0
    no_model: list[str] = field(default_factory=list)
    netless_pads: int = 0
    edge_items: int = 0

    def to_json(self) -> dict:
        return {"copper_layers": self.copper_layers,
                "layer_names": self.layer_names,
                "copper": dict(self.copper), "parts": self.footprints,
                "parts_bottom": self.footprints_bottom,
                "nets": len(self.nets), "vias": self.vias,
                "zones": self.zones, "zones_unfilled": self.zones_unfilled,
                "no_3d_model": len(self.no_model),
                "netless_pads": self.netless_pads,
                "edge_items": self.edge_items}


_TOP = re.compile(r"^\t\((\w+)", re.M)
_LAYER = re.compile(r'\(layer "([^"]+)"')
_LAYERS = re.compile(r'\(layers((?: "[^"]+")+)\)')
_NET = re.compile(r'\(net (?:\d+ )?"([^"]*)"\)')


def _blocks(text: str):
    """Top-level (one-tab) items of a KiCad-formatted board, as (kind, text)."""
    starts = [(m.start(), m.group(1)) for m in _TOP.finditer(text)]
    for i, (pos, kind) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        yield kind, text[pos:end]


def read_board(path: Path) -> Board:
    text = path.read_text(encoding="utf-8")
    b = Board()
    for kind, blk in _blocks(text):
        if kind == "layers":
            for m in re.finditer(r'\(\d+ "([^"]+)" (\w+)(?: "([^"]*)")?\)', blk):
                name, typ, user = m.groups()
                if name.endswith(".Cu"):
                    b.copper_layers.append(name)
                    if user:
                        b.layer_names[name] = user
        elif kind in ("segment", "arc"):
            m = _LAYER.search(blk)
            if m:
                b.copper[m.group(1)] += 1
            n = _NET.search(blk)
            if n and n.group(1):
                b.nets.add(n.group(1))
        elif kind == "via":
            b.vias += 1
        elif kind == "zone":
            if "(keepout" in blk:
                continue
            b.zones += 1
            m = _LAYER.search(blk[:600])
            lays = [m.group(1)] if m else []
            ml = _LAYERS.search(blk[:600])
            if ml:
                lays = re.findall(r'"([^"]+)"', ml.group(1))
            for lay in lays:
                if lay.endswith(".Cu"):
                    b.copper[lay] += 1
            if "(filled_polygon" not in blk:
                b.zones_unfilled += 1
        elif kind == "footprint":
            b.footprints += 1
            head = blk[:400]
            m = _LAYER.search(head)
            if m and m.group(1) == "B.Cu":
                b.footprints_bottom += 1
            r = re.search(r'\((?:property "Reference"|fp_text reference) "?([^"\s)]*)', blk)
            ref = r.group(1) if r else ""
            b.refs.append(ref)
            if re.search(r"\(attr[^)]*\bboard_only\b", blk) or not re.search(r"\n\t\t\(pad ", blk):
                # board-only, or nothing to place (a logo or frame with a
                # symbol but no pads): no component record in ODB++
                b.board_only.add(ref)
            if "(model " not in blk and re.search(r"\(pad ", blk) and "(attr" in blk and "board_only" not in blk:
                b.no_model.append(ref)
            for pad in re.finditer(r"\n\t\t\(pad \"[^\"]*\" (\w+)(.*?)(?=\n\t\t\(|\n\t\)|\Z)", blk, re.S):
                if pad.group(1) == "np_thru_hole":
                    continue
                n = _NET.search(pad.group(2))
                if n and n.group(1):
                    b.nets.add(n.group(1))
                else:
                    b.netless_pads += 1
        elif kind.startswith("gr_"):
            m = _LAYER.search(blk)
            if m and m.group(1) == "Edge.Cuts":
                b.edge_items += 1
    return b


# ------------------------------------------------------------- comparison

@dataclass
class Loss:
    level: str      # lost | check | note
    what: str       # copper | parts | nets | layer | rules | schematic | ...
    text: str

    def to_json(self) -> dict:
        return {"level": self.level, "what": self.what, "text": self.text}


_SKIP_PATTERNS = (
    re.compile(r"Layer '([^']+)' could not be mapped and will be skipped"),
    re.compile(r"No user layer to put layer (.+?)\s*$"),
)


def skipped_layers(log: str) -> list[str]:
    out = []
    for line in log.splitlines():
        for pat in _SKIP_PATTERNS:
            m = pat.search(line)
            if m:
                out.append(m.group(1).strip())
    return out


def other_warnings(log: str) -> list[str]:
    out = []
    for line in log.splitlines():
        if not re.search(r"\b(Warning|Error)\b", line):
            continue
        if any(p.search(line) for p in _SKIP_PATTERNS):
            continue
        out.append(re.sub(r"^\d\d:\d\d:\d\d: ", "", line.strip()))
    return out


def _sample(items, n: int = 8) -> str:
    items = sorted(items)
    more = f" and {len(items) - n} more" if len(items) > n else ""
    return ", ".join(items[:n]) + more


def compare(src: Source | None, board: Board, report: dict, log: str) -> list[Loss]:
    """Everything the import lost or changed, worst first."""
    losses: list[Loss] = []
    mapping = {k: (v or {}).get("kicad_layer") for k, v in (report.get("layer_mapping") or {}).items()}
    skipped = skipped_layers(log)
    by_user = {v: k for k, v in board.layer_names.items()}

    def landed(name: str) -> str | None:
        """The KiCad copper layer a source layer landed on, if any."""
        if name in skipped:
            return None
        lay = mapping.get(name)
        if lay:
            return lay if lay.endswith(".Cu") else None
        return by_user.get(name) or (src.kicad_names.get(name) if src else None)

    if src is not None and src.copper:
        for name, n in src.copper.items():
            if n <= 0:
                continue
            lay = landed(name)
            if lay is None:
                where = ("KiCad skipped it" if name in skipped else
                         f"KiCad put it on {mapping[name]}, not copper" if mapping.get(name) else
                         "no copper layer in the import carries its name")
                losses.append(Loss("lost", "copper",
                                   f"copper layer {name!r} held {n} objects in the source; {where}"))
            elif not board.copper.get(lay):
                losses.append(Loss("lost", "copper",
                                   f"copper layer {name!r} held {n} objects in the source; "
                                   f"it landed on {lay}, which is empty in the import"))
        used = sum(1 for n in src.copper.values() if n > 0)
        got = sum(1 for lay in board.copper_layers if board.copper.get(lay))
        if got < used and not any(l.what == "copper" for l in losses):
            losses.append(Loss("lost", "copper",
                               f"the source has copper on {used} layers; the import on {got}"))
    if src is not None and src.copper_declared and src.copper_declared != len(board.copper_layers):
        losses.append(Loss("check", "layers",
                           f"the source declares {src.copper_declared} copper layers; "
                           f"the import's stackup has {len(board.copper_layers)}"))

    if src is not None and src.refs:
        missing = src.refs - set(board.refs)
        extra = set(board.refs) - src.refs
        renamed = {}
        pcb_named = {}
        for r in sorted(missing):  # Altium: the PCB shows another designator
            shown = src.pcb_refs.get(r)
            if shown and (shown in board.refs or any(
                    re.fullmatch(re.escape(shown) + r"_?\d+", e) for e in board.refs)):
                pcb_named[r] = shown
        missing -= set(pcb_named)
        extra -= {e for e in extra if any(e == v or re.fullmatch(re.escape(v) + r"_?\d+", e)
                                          for v in pcb_named.values())}
        if pcb_named:
            losses.append(Loss("check", "parts",
                               f"{len(pcb_named)} parts carry a PCB designator that isn't "
                               "their schematic designator (Altium SOURCEDESIGNATOR); the "
                               "import takes the PCB's, so board and schematic won't pair "
                               "until they're renamed back: " +
                               _sample(f"{a} shown as {b}" for a, b in pcb_named.items())))
        for r in sorted(missing):
            # KiCad gives a reference with no number one ("PWR" -> "PWR0")
            cand = [e for e in extra if re.fullmatch(re.escape(r) + r"_?\d+", e)]
            if len(cand) == 1:
                renamed[r] = cand[0]
                extra.discard(cand[0])
        missing -= set(renamed)
        if renamed:
            losses.append(Loss("check", "parts",
                               f"{len(renamed)} references renamed by the import: " +
                               _sample(f"{a} -> {b}" for a, b in renamed.items())))
        if missing:
            losses.append(Loss("lost", "parts",
                               f"{len(missing)} of {len(src.refs)} parts didn't come in by "
                               f"reference: {_sample(missing)}"))
        dup = [r for r, c in Counter(board.refs).items() if c > 1 and r]
        if dup:
            losses.append(Loss("check", "parts",
                               f"{len(dup)} references appear more than once in the import: {_sample(dup)}"))
    if src is not None and src.nets:
        missing = src.nets - board.nets
        if missing:
            losses.append(Loss("check", "nets",
                               f"{len(missing)} of {len(src.nets)} source nets aren't on any "
                               f"pad or track of the import (renamed, or had no copper): "
                               f"{_sample(missing)}"))
    if src is not None and src.vias is not None and src.vias != board.vias:
        losses.append(Loss("check", "vias",
                           f"the source has {src.vias} vias; the import {board.vias}"))

    empty, drawn, unknown = [], [], []
    for name in skipped:
        if src is not None and src.copper and src.copper.get(name, 0) > 0:
            continue  # said above as copper
        if src is not None and (src.other.get(name) or 0) > 0:
            drawn.append(f"{name} ({src.other[name]})")
        elif src is not None and ((src.copper is not None and name in src.copper)
                                  or src.other):
            empty.append(name)
        else:
            unknown.append(name)
    if drawn:
        losses.append(Loss("check", "layer",
                           f"{len(drawn)} non-copper layers KiCad skipped held drawings, "
                           f"now gone (records in the source): {_sample(drawn, 12)}"))
    if unknown:
        losses.append(Loss("check", "layer",
                           f"{len(unknown)} layers KiCad skipped, not counted in the source "
                           f"(non-copper or unused; their drawings, if any, are gone): "
                           f"{_sample(unknown, 12)}"))
    if empty:
        losses.append(Loss("note", "layer",
                           f"{len(empty)} empty layers skipped: {_sample(empty, 12)}"))
    for w in other_warnings(log):
        losses.append(Loss("check", "importer", w))
    for e in report.get("errors") or []:
        losses.append(Loss("lost", "importer", str(e)))

    if board.edge_items == 0:
        losses.append(Loss("check", "outline", "no board outline on Edge.Cuts in the import "
                           "(if the source had one, it was lost)"))
    if board.netless_pads:
        losses.append(Loss("check", "nets",
                           f"{board.netless_pads} copper pads have no net in the import (pins left unconnected in the source, or nets lost)"))
    if board.no_model:
        losses.append(Loss("check", "3d",
                           f"{len(board.no_model)} of {board.footprints} parts have no 3D model: "
                           f"{_sample(board.no_model)}"))
    if board.zones_unfilled:
        losses.append(Loss("note", "zones",
                           f"{board.zones_unfilled} of {board.zones} copper zones are unfilled; "
                           "the gates refill them, but check a pour's shape against the source"))
    if src is not None:
        losses.extend(Loss("note", "source", n) for n in src.notes)
    order = {"lost": 0, "check": 1, "note": 2}
    losses.sort(key=lambda l: order[l.level])
    return losses
