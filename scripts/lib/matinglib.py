"""matinglib - can every mating connector on the board actually be mated?

Shared by the P6 place gate (place_metrics.py, family "mating") and the P8
verify gate (check_mating.py), so the fault fails at placement and stays
failed if a later edit brings it back.

Which footprints are mating connectors, and their plug geometry, come from
reference/connector_mating.yaml (see its header). Three violation kinds, all
error severity, each naming the connector in `connector` (refs also lists
the blocking parts):

  mating_faces_inward  a horizontal connector's mouth faces away from its
                       nearest board edge (PCB-0021-A J4: USB-A with its back
                       at the edge and its mouth toward L1).
  mating_mouth_inset   the mouth faces the nearest edge but sits more than
                       mouth_tol_mm inside it.
  mating_zone_blocked  a part on the connector's side sits in the insertion
                       zone in front of the mouth (horizontal), or a part
                       taller than tall_mm sits within finger_mm of a
                       vertical connector.

  mating_model_reversed  a horizontal connector's 3D model points the other
                       way from its copper: the model lies behind the contact
                       pads, so every render shows the mouth facing into the
                       board while the pads face the edge, or the other way
                       round (PCB-0023-A J101/J102/J501: an edge-launch SMA
                       whose model is turned 180 degrees).

A connector whose mate lies on the board itself (an FPC socket taking the
tail of a display that sits on this board) does not mate from an edge, so
the board's constraints.json declares it under placement.mates:

  {"ref": "J2", "mate": "onboard_flex",
   "corridor": [[x1, y1, x2, y2], ...], "mate_refs": ["DS1"],
   "reason": "the LCD's tail lies on the top side into J2"}

The corridor is the rectangles the flex lies over (its real outline, board
mm). For a declared connector the edge rule (faces_inward, mouth_inset) and
the plug-shaped insertion zone give way to two errors:

  mating_mate_misaligned  the corridor does not cross the connector's mouth
                       face, so the declared flex cannot reach the socket.
  mating_zone_blocked  a part on the connector's side (other than
                       mate_refs, the parts that are the mate) sits in the
                       corridor.

and mating_mate_invalid for an entry that names no part on the board, an
unknown mate kind, or no corridor. model_reversed still applies.

There is also one warning, mating_direction_unknown, for a horizontal connector whose
pad layout is symmetric about its body: it asks for a `mouth` entry in the
table rather than guessing.

Mouth direction is read from the pad layout: the contact pads (the most
common pad size) sit at the back of the housing and the body extends toward
the mouth, so the mouth points from the contact-pad centroid toward the
courtyard centroid. The table's `mouth` map overrides that per footprint.

A connector whose SMD pads sit on both outer copper layers straddles the
board edge (an edge-launch SMA, a card-edge socket), so it is checked as
horizontal even when its name matched a vertical family: a name is a hint,
the copper is the part.

The 3D model's direction is read against the same contact pads: its
bounding box, put through the footprint's model offset/rotate/scale the way
KiCad's 3D viewer does, must reach ahead of them toward the mouth. A box
cannot tell a model turned round from one pushed back, so only a model that
lies wholly behind the contacts fails (model_reversed); one that is merely
offset passes. Only a .wrl is read (beside the board, or the .wrl twin of a .step
model); a connector without one is listed in the facts, never failed.

Part heights (vertical rule) come from the 3D model: a "-H<mm>" in the model
or footprint name, else the highest vertex of a .wrl model found beside the
board (VRML units are 2.54 mm). A part with no readable height is listed in
the facts, never failed.
"""
from __future__ import annotations

import itertools
import math
import re
from functools import lru_cache
from pathlib import Path

import yaml
from shapely.geometry import LineString, box

import checklib
from geom import _is_node, _kid, _kids, _nums, _strs, _tok, fp_field

SOURCE = "matinglib"
TABLE = Path(__file__).resolve().parents[2] / "reference" / "connector_mating.yaml"
EPS_AREA = 0.01
AXES = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}
KINDS = ("mating_faces_inward", "mating_mouth_inset", "mating_zone_blocked",
         "mating_model_reversed", "mating_mate_misaligned",
         "mating_mate_invalid", "mating_direction_unknown",
         "mating_mouth_disagrees")
# reference designators that name a connector: only these are matched on
# their value, MPN and LCSC category as well as the footprint name (#953)
CONNECTOR_REF = re.compile(r"^(J|P|CN|CON)\d+$")
# footprint properties that hold the manufacturer part number (lower case,
# spaces, dots and underscores dropped)
MPN_PROPS = ("mpn", "manufacturerpartnumber", "mfrpart", "mfrpartnumber",
             "mfrpart#", "manufacturerpart", "partnumber", "mfrpn")
# mechanical.entry.contacts_at in a part's extraction: where the contact
# tails sit along the plug's way in (#952)
CONTACTS_AT = ("back", "mouth")
# a connector whose courtyard comes this close to the board outline is an
# edge connector: one no family knows can't be shown to face off the board
# (check_mating's edge_unjudged; an error at release, #911)
EDGE_NEAR_MM = 3.0
# what placement.mates[].mate may name: the mate lies on this board
MATES = ("onboard_flex",)
# a declared family for a mates entry whose footprint name matches none
DECLARED_FAMILY = {"family": "declared", "entry": "horizontal"}
# a horizontal connector's 3D model counts as reversed when it reaches less
# than MODEL_AHEAD_MM ahead of the contact pads along the copper's mouth
# while it is at least MODEL_MIN_LEN_MM long on that axis (model_reversed)
MODEL_AHEAD_MM = 1.0
MODEL_MIN_LEN_MM = 3.0


@lru_cache(maxsize=1)
def load_table(path: str = str(TABLE)) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    d = doc.get("defaults", {})
    fams = []
    for fam in doc.get("families", []):
        fams.append({**d, **fam, "re": re.compile(fam["match"], re.I)})
    mouth = [(re.compile(k, re.I), v) for k, v in (doc.get("mouth") or {}).items()]
    by_name = {f["family"]: f for f in fams}
    cats = []
    for k, v in (doc.get("categories") or {}).items():
        if v not in by_name:
            raise checklib.CheckError(
                f"{path}: categories '{k}' names no family '{v}'")
        cats.append((re.compile(k, re.I), by_name[v]))
    return {"families": fams, "mouth": mouth, "defaults": d,
            "categories": cats}


def _name(fpid: str) -> str:
    return fpid.split(":", 1)[-1]


def family_of(fpid: str, table: dict | None = None) -> dict | None:
    table = table or load_table()
    return _family_of_text(_name(fpid), table)


def _family_of_text(text: str, table: dict) -> dict | None:
    for fam in table["families"]:
        if text and fam["re"].search(text):
            return fam
    return None


def classify(ref: str, fpid: str, props: dict | None, row: dict | None,
             table: dict | None = None) -> tuple[dict | None, str | None]:
    """(family, what it was matched on) for one footprint (#953).

    The footprint name first, as always. A connector reference
    (CONNECTOR_REF) that it leaves unmatched is tried on its Value, then its
    MPN (a footprint property, else its parts.json row), then on the row's
    LCSC category (JLC's componentTypeEn, else the end of the row's LCSC
    description) through the yaml's `categories` table: EasyEDA names such
    as SIM-SMD_8P-1040310811 hide what the part is, but its value says
    microSD and LCSC files it under the card sockets."""
    table = table or load_table()
    fam = family_of(fpid, table)
    if fam is not None:
        return fam, "footprint"
    if not CONNECTOR_REF.match(ref):
        return None, None
    props, row = props or {}, row or {}
    for on, text in (("value", props.get("Value", "")),
                     ("mpn", part_mpn(props, row))):
        fam = _family_of_text(text, table)
        if fam is not None:
            return fam, on
    cat = row.get("category") or row.get("description") or ""
    for rx, fam in table["categories"]:
        if cat and rx.search(cat):
            return fam, "category"
    return None, None


def part_mpn(props: dict, row: dict | None = None) -> str:
    for k, v in props.items():
        if re.sub(r"[\s._-]", "", k.lower()) in MPN_PROPS and v.strip():
            return v.strip()
    return str((row or {}).get("mpn") or "")


def load_parts(parts: Path | None) -> dict:
    """refdes -> parts.json row (the P3 BOM) from a board's parts dir;
    empty when there is none."""
    rows: dict = {}
    if parts is None:
        return rows
    pj = Path(parts) / "parts.json"
    if pj.is_file():
        doc = checklib.load_json(pj, "parts.json")
        items = doc.get("parts", []) if isinstance(doc, dict) else doc
        for row in items if isinstance(items, list) else []:
            if not isinstance(row, dict):
                continue
            refs = row.get("refdes") or row.get("refs") or []
            for r in [refs] if isinstance(refs, str) else refs:
                rows[str(r)] = row
    return rows


def mechanical_of(parts: Path | None, lcsc: str) -> dict:
    """The `mechanical` block of LCSC part `lcsc`'s extraction, or {}: the
    board's parts dir first, then reference/extractions."""
    if not lcsc:
        return {}
    dirs = ([Path(parts)] if parts else []) + [TABLE.parent / "extractions"]
    for d in dirs:
        f = d / f"{lcsc}.json"
        if f.is_file():
            ext = checklib.load_json(f, f"extraction {f.name}")
            return (ext.get("mechanical") or {}) if isinstance(ext, dict) \
                else {}
    return {}


def _snap(dx: float, dy: float) -> str:
    if abs(dx) >= abs(dy):
        return "+x" if dx >= 0 else "-x"
    return "+y" if dy >= 0 else "-y"


def contact_centroid(f) -> tuple[float, float] | None:
    """Local centroid of the contact pads (the most common pad size): the
    back of the housing."""
    pads = [p for p in f.pads if p.number]
    if not pads:
        return None
    sizes: dict = {}
    for p in pads:
        k = (round(p.size[0], 2), round(p.size[1], 2), p.through)
        sizes.setdefault(k, []).append(p)
    contacts = max(sizes.values(), key=len)
    return (sum(p.local[0] for p in contacts) / len(contacts),
            sum(p.local[1] for p in contacts) / len(contacts))


def mouth_local(f, table: dict | None = None) -> str | None:
    """The mouth direction in the footprint's local frame, or None when the
    pad layout is symmetric about the body (nothing to read it from)."""
    table = table or load_table()
    for rx, d in table["mouth"]:
        if rx.search(_name(f.fpid)):
            return d
    return pad_mouth(f)


OPPOSITE = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}


def mouth_reading(f, table: dict | None = None,
                  mech: dict | None = None) -> tuple[str | None, dict]:
    """(mouth direction in the footprint's local frame, how it was read).

    The part's datasheet extraction comes first (#952): its
    mechanical.entry.contacts_at says whether the contact tails sit at the
    back of the housing (the pad rule's reading) or at the mouth (the
    opposite: Molex 104031 inner-lead microSD, many push-push SD and SIM
    sockets). The yaml `mouth` override and then the pad rule are the
    fallback. The second value records {"from": "extraction" | "override"
    | "pads", "contacts_at", and "table": what the override or pad rule
    reads when the extraction overrules it}."""
    table = table or load_table()
    table_d = mouth_local(f, table)
    over = any(rx.search(_name(f.fpid)) for rx, _ in table["mouth"])
    how = {"from": "override" if over else "pads"}
    entry = (mech or {}).get("entry") or {}
    at = entry.get("contacts_at") if isinstance(entry, dict) else None
    if at in CONTACTS_AT:
        pads = pad_mouth(f)
        if pads is not None:
            d = pads if at == "back" else OPPOSITE[pads]
            how = {"from": "extraction", "contacts_at": at}
            if table_d is not None and table_d != d:
                how["table"] = table_d
                how["table_from"] = "override" if over else "pads"
            return d, how
    if table_d is None:
        how = {"from": None}
    return table_d, how


def pad_mouth(f) -> str | None:
    """The pad rule alone: from the contact-pad centroid toward the body's,
    or None when they (nearly) coincide."""
    cc = contact_centroid(f)
    if cc is None:
        return None
    cx, cy = cc
    body = f.extents_local().centroid
    dx, dy = body.x - cx, body.y - cy
    if math.hypot(dx, dy) < 0.3:
        return None
    return _snap(dx, dy)


def edge_gap_mm(model, f) -> float | None:
    """How far the footprint's courtyard stays inside the board outline at
    its closest (0 or less: it reaches or crosses the edge), or None with no
    outline."""
    outline = getattr(model, "outline", None)
    if outline is None or outline.is_empty:
        return None
    body = f.extents_abs()
    if not outline.contains(body):
        return 0.0
    return float(outline.exterior.distance(body))


def _to_abs_dir(f, d: str) -> tuple[float, float]:
    ux, uy = AXES[d]
    o = f.to_abs((0.0, 0.0))
    p = f.to_abs((ux, uy))
    return AXES[_snap(p[0] - o[0], p[1] - o[1])]


def _span(poly, u: tuple[float, float]) -> tuple[float, float]:
    vals = [x * u[0] + y * u[1] for x, y in poly.exterior.coords]
    return min(vals), max(vals)


def _edge_gap(outline, body, u) -> float:
    """Distance from the body's face in direction u to the board edge it
    faces (negative when the face overhangs that edge)."""
    c = body.centroid
    lo, hi = _span(body, u)
    ray = LineString([(c.x, c.y), (c.x + u[0] * 1e4, c.y + u[1] * 1e4)])
    hit = ray.intersection(outline.exterior)
    pts = [g.centroid for g in getattr(hit, "geoms", [hit]) if not g.is_empty]
    ts = [p.x * u[0] + p.y * u[1] for p in pts]
    ts = [t for t in ts if t > c.x * u[0] + c.y * u[1]]
    if not ts:  # centroid already off the board in this direction
        return lo - hi
    return min(ts) - hi


# Courtyards carry a margin the parts don't. A neighbour's courtyard reaching
# this far past the edge beside the mouth, or this far across the mouth's
# courtyard width, touches neither hand nor plug (eda-crossover's tact switches
# flank its Qwiic socket flush with the edge: 0.05 mm over it, 0.12 mm across).
GRIP_EDGE_TOL_MM = 0.25


def _zone(body, u, gap: float, plug: float, grip: float):
    """Insertion zone: the mouth's own width from the mouth face to the board
    edge (the plug slides over that ledge), then that width plus grip each
    side out to plug + grip past the edge, where the hand holds it."""
    v = (-u[1], u[0])
    _, front = _span(body, u)
    wlo, whi = _span(body, v)
    edge = front + max(gap, 0.0)

    def rect(a0, a1, b0, b1):
        pts = [(a * u[0] + b * v[0], a * u[1] + b * v[1])
               for a, b in ((a0, b0), (a1, b1))]
        return box(min(pts[0][0], pts[1][0]), min(pts[0][1], pts[1][1]),
                   max(pts[0][0], pts[1][0]), max(pts[0][1], pts[1][1]))

    zone = rect(edge + GRIP_EDGE_TOL_MM, edge + plug + grip, wlo - grip,
                whi + grip)
    return zone.union(rect(front, edge + GRIP_EDGE_TOL_MM,
                           wlo + GRIP_EDGE_TOL_MM, whi - GRIP_EDGE_TOL_MM))


# ------------------------------------------------------------ part heights

_H_RE = re.compile(r"[-_]H(\d+(?:\.\d+)?)(?![\d.])", re.I)


def _xyz(node, key: str, default: list[float]) -> list[float]:
    k = _kid(node, key)
    if k is None or _kid(k, "xyz") is None:
        return default
    v = _nums(_kid(k, "xyz"))
    return v if len(v) == 3 else default


def board_meta(pcb: Path) -> dict[str, dict]:
    """ref -> {"model": (first 3D model path, its rotate xyz) or None,
    "offset"/"scale": that model's xyz, "smd_sides": the outer copper layers
    ({"F", "B"}) its SMD pads sit on, "props": its properties by name}
    from the board file."""
    import sexpdata
    tree = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
    out = {}
    for fp in _kids(tree, "footprint"):
        props = {}
        for prop in _kids(fp, "property"):
            s = _strs(prop)
            if len(s) >= 2:
                props.setdefault(s[0], s[1])
        ref = props.get("Reference") or fp_field(fp, "Reference")
        if not ref:
            continue
        sides = set()
        for pad in _kids(fp, "pad"):
            if "smd" not in [_tok(t) for t in pad[1:4] if not _is_node(t)]:
                continue
            lay = _kid(pad, "layers")
            for name in (_strs(lay) if lay is not None else []):
                if name in ("F.Cu", "B.Cu"):
                    sides.add(name[0])
        meta = {"model": None, "offset": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0], "smd_sides": frozenset(sides),
                "props": props}
        m = _kid(fp, "model")
        if m is not None and _strs(m):
            meta["model"] = (_strs(m)[0], _xyz(m, "rotate", [0.0, 0.0, 0.0]))
            meta["scale"] = _xyz(m, "scale", [1.0, 1.0, 1.0])
            if _kid(m, "offset") is not None:
                meta["offset"] = _xyz(m, "offset", [0.0, 0.0, 0.0])
            else:  # KiCad 5: (at (xyz)) in inches
                meta["offset"] = [v * 25.4 for v in
                                  _xyz(m, "at", [0.0, 0.0, 0.0])]
        out[ref] = meta
    return out


def model_paths(pcb: Path) -> dict[str, tuple[str, list[float]]]:
    """ref -> (first 3D model path, its rotate xyz) from the board file."""
    return {r: m["model"] for r, m in board_meta(pcb).items() if m["model"]}


def straddles(meta: dict | None) -> bool:
    """SMD pads on both outer copper layers: the part clamps the board edge
    between its legs, so its plug goes in parallel to the board."""
    return bool(meta) and meta["smd_sides"] >= {"F", "B"}


@lru_cache(maxsize=512)
def _wrl_bbox(path: str) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """(min xyz, max xyz) of a .wrl's vertices in mm (VRML unit 2.54 mm);
    transforms inside the file are not applied."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    xs, ys, zs = [], [], []
    for blk in re.findall(r"point\s*\[([^\]]*)\]", text):
        v = [float(x) for x in
             re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", blk)]
        n = len(v) // 3 * 3
        xs.extend(v[0:n:3])
        ys.extend(v[1:n:3])
        zs.extend(v[2:n:3])
    if not zs:
        return None
    return (tuple(min(a) * 2.54 for a in (xs, ys, zs)),
            tuple(max(a) * 2.54 for a in (xs, ys, zs)))


def _wrl_height(path: str) -> float | None:
    bb = _wrl_bbox(path)
    return bb[1][2] if bb else None


def find_wrl(model_path: str, pcb: Path) -> Path | None:
    """The .wrl a board's model path points at: the path itself, else the
    same name under the workspace's lib/*.3dshapes/ (a path baked in another
    checkout), trying the .wrl twin of a .step model."""
    raw = model_path.replace("${KIPRJMOD}", str(Path(pcb).parent))
    p = Path(raw)
    if p.suffix.lower() in (".step", ".stp"):
        p = p.with_suffix(".wrl")
    if p.suffix.lower() != ".wrl":
        return None
    ws = Path(pcb).resolve().parent.parent
    for cand in [p, *ws.glob(f"lib/*.3dshapes/{p.name}")]:
        if cand.is_file():
            return cand
    return None


def _rot3(p, axis: int, deg: float):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    x, y, z = p
    if axis == 0:
        return (x, y * c - z * s, y * s + z * c)
    if axis == 1:
        return (x * c + z * s, y, -x * s + z * c)
    return (x * c - y * s, x * s + y * c, z)


def model_corners_local(f, meta: dict | None,
                        pcb: Path) -> list[tuple[float, float]] | None:
    """Where the 8 corners of the 3D model's bounding box land in the
    footprint's local frame (as the file stores it), or None when no .wrl
    is found.

    KiCad's 3D viewer places a model as offset + Rz(-rz).Ry(-ry).Rx(-rx).
    scale . p in a y-up frame, so footprint-local y is the negated 3D y. A
    back-side footprint's file locals are the library's mirrored in y and its
    model keeps its library transform, so there the 3D y is taken as is
    (checked against kicad-cli renders: PCB-0023-A's SMA, ry 270, and
    PCB-0025-A's back-side barrel jack, rz 90)."""
    if not meta or not meta["model"]:
        return None
    wrl = find_wrl(meta["model"][0], pcb)
    bb = _wrl_bbox(str(wrl)) if wrl else None
    if bb is None:
        return None
    rx, ry, rz = meta["model"][1]
    out = []
    for corner in itertools.product(*zip(*bb)):
        p = tuple(c * s for c, s in zip(corner, meta["scale"]))
        p = _rot3(_rot3(_rot3(p, 0, -rx), 1, -ry), 2, -rz)
        x, y = p[0] + meta["offset"][0], p[1] + meta["offset"][1]
        out.append((x, y) if f.side == "back" else (x, -y))
    return out


def model_span_mm(f, d: str, meta: dict | None, pcb: Path,
                  contacts_at: str | None = None
                  ) -> tuple[float, float] | None:
    """How far the 3D model reaches behind (first, negative) and ahead of
    (second) the contact pads along the copper's mouth direction `d`.

    None when the contact pads sit at the front of the body (a `mouth`
    override that points toward them, as on Molex 104031 inner-lead
    microSD) and no extraction says so: the measure there is read by
    model_reversed only when `contacts_at` is "mouth" (#911, #952)."""
    corners = model_corners_local(f, meta, pcb)
    cc = contact_centroid(f)
    if corners is None or cc is None:
        return None
    u = AXES[d]
    body = f.extents_local().centroid
    if contacts_at != "mouth" and \
            (cc[0] - body.x) * u[0] + (cc[1] - body.y) * u[1] > 0.3:
        return None
    along = [(x - cc[0]) * u[0] + (y - cc[1]) * u[1] for x, y in corners]
    return min(along), max(along)


def model_reversed(span: tuple[float, float] | None,
                   contacts_at: str | None = None) -> bool:
    """The model lies behind the contact pads: it reaches less than
    MODEL_AHEAD_MM ahead of them while it is MODEL_MIN_LEN_MM or longer.

    Only the bounding box is read, and that cannot tell a model turned
    round from one pushed back: a box is the same shape either way. So
    this asks the one thing a box does answer. A connector's contacts sit at
    the back of its housing, so a model of any length must reach forward of
    them. A model that is merely offset still does (PCB-0025-A J101 sits
    5.6 mm too far back and reaches 5 mm ahead; PCB-0016-A J1 2.2 mm).

    With `contacts_at` "mouth" (the extraction says the contacts sit at
    the front, #952) the housing lies behind them instead, so the model is
    reversed when it reaches less than MODEL_AHEAD_MM behind them."""
    if span is None:
        return False
    lo, hi = span
    if contacts_at == "mouth":
        return lo > -MODEL_AHEAD_MM and hi - lo >= MODEL_MIN_LEN_MM
    return hi < MODEL_AHEAD_MM and hi - lo >= MODEL_MIN_LEN_MM


def part_height(f, model: tuple[str, list[float]] | None,
                pcb: Path) -> float | None:
    for name in ([Path(model[0]).stem] if model else []) + [_name(f.fpid)]:
        m = _H_RE.search(name)
        if m:
            return float(m.group(1))
    if not model or not model[0].lower().endswith(".wrl"):
        return None
    if any(abs(r) > 1e-6 for r in model[1][:2]):
        return None  # tilted model: its z is not the board's z
    raw = model[0].replace("${KIPRJMOD}", str(Path(pcb).parent))
    base = Path(raw).name
    ws = Path(pcb).resolve().parent.parent
    for cand in [Path(raw), *ws.glob(f"lib/*.3dshapes/{base}")]:
        if cand.is_file():
            return _wrl_height(str(cand))
    return None


# ------------------------------------------------------------ declared mates

def declared_mates(placement: dict | None, fps: dict) -> tuple[dict, list]:
    """(ref -> its valid placement.mates entry, violations for the invalid
    ones). A bad entry is an error: a typo must not quietly drop a socket
    back onto the edge rule or out of the check."""
    good, bad = {}, []
    for i, m in enumerate((placement or {}).get("mates") or []):
        m = m if isinstance(m, dict) else {}
        ref = m.get("ref")
        rects = m.get("corridor")
        why = None
        if ref not in fps:
            why = f"names {ref!r}, which is not on the board"
        elif m.get("mate") not in MATES:
            why = (f"mate {m.get('mate')!r} is not one of "
                   f"{', '.join(MATES)}")
        elif not (isinstance(rects, list) and rects and all(
                isinstance(r, list) and len(r) == 4
                and all(isinstance(x, (int, float)) for x in r)
                for r in rects)):
            why = "needs a corridor: a list of [x1, y1, x2, y2] rectangles"
        elif ref in good:
            why = f"declares {ref} a second time"
        if why:
            bad.append(checklib.violation(
                "place", "error", None, None, None,
                [ref] if isinstance(ref, str) else [],
                f"placement.mates[{i}] {why}", SOURCE,
                kind="mating_mate_invalid",
                connector=ref if isinstance(ref, str) else None))
            continue
        good[ref] = m
    return good, bad


def corridor_poly(mate: dict):
    from shapely.ops import unary_union
    return unary_union([box(min(x1, x2), min(y1, y2), max(x1, x2),
                            max(y1, y2)) for x1, y1, x2, y2 in
                        mate["corridor"]])


def _mouth_strip(body, u, tol: float):
    """The connector's mouth face, tol either side: a flex that reaches the
    socket crosses it."""
    v = (-u[1], u[0])
    _, front = _span(body, u)
    wlo, whi = _span(body, v)
    pts = [(a * u[0] + b * v[0], a * u[1] + b * v[1])
           for a, b in ((front - tol, wlo), (front + tol, whi))]
    return box(min(pts[0][0], pts[1][0]), min(pts[0][1], pts[1][1]),
               max(pts[0][0], pts[1][0]), max(pts[0][1], pts[1][1]))


def _onboard(ref, fam, f, d, u, body, others, mate, fact, out):
    """The declared-mate rules for one horizontal connector: the corridor
    crosses its mouth face, and no part but the mate sits in it."""
    corridor = corridor_poly(mate)
    c = f.center_abs()
    fact.update(mate=mate["mate"],
                corridor_mm2=checklib.rnd(corridor.area, 1))
    if d is not None:
        facing = _snap(*u)
        fact["mouth"] = facing
        strip = _mouth_strip(body, u, fam["mouth_tol_mm"])
        if corridor.intersection(strip).area <= EPS_AREA:
            out.append(checklib.violation(
                "place", "error", c, None, None, [ref],
                f"{ref} ({fam['family']}): its declared {mate['mate']} "
                f"corridor does not reach its mouth (facing {facing}), so "
                "the flex cannot go in - turn the socket to face the "
                "corridor or fix the corridor in placement.mates", SOURCE,
                kind="mating_mate_misaligned", connector=ref, mouth=facing))
    skip = set(mate.get("mate_refs") or [])
    blockers = sorted(o.ref for o in others if o.ref not in skip
                      and o.extents_abs().intersects(corridor)
                      and o.precise_extents_abs().intersection(
                          corridor).area > EPS_AREA)
    fact["blocked_by"] = blockers
    if blockers:
        z = corridor.centroid
        out.append(checklib.violation(
            "place", "error", (z.x, z.y), None, None, [ref, *blockers],
            f"{ref} ({fam['family']}): {', '.join(blockers)} sit in the "
            f"declared corridor its {mate['mate']} mate lies over", SOURCE,
            kind="mating_zone_blocked", connector=ref, blocked_by=blockers))


# ------------------------------------------------------------ the check

def _reversed(ref, fam, c, u, span, contacts=None) -> dict:
    facing, back = _snap(*u), _snap(-u[0], -u[1])
    where = (f"the model lies ahead of them, {span[0]:.1f} mm to "
             f"{span[1]:.1f} mm forward" if contacts == "mouth" else
             f"the model lies behind them, {-span[0]:.1f} mm back to "
             f"{-span[1]:.1f} mm")
    return checklib.violation(
        "place", "error", c, None, None, [ref],
        f"{ref} ({fam['family']}): its 3D model points the "
        f"other way from its copper - the pads put the mouth "
        f"at {facing} but {where}, so every "
        f"render shows it facing {back}. One of the two is "
        "backwards: check the datasheet, then fix the model's "
        "rotate in the footprint (or the footprint)", SOURCE,
        kind="mating_model_reversed", connector=ref,
        mouth=facing, model_mouth=back,
        model_span_mm=[checklib.rnd(v, 2) for v in span])


def edge_unjudged(model, facts: list[dict]) -> list[dict]:
    """The J/P/CN parts within EDGE_NEAR_MM of the outline that no family
    judged (in `facts`, from violations()): nothing shows which way they mate.
    check_mating lists them (an error at release, #911) and the place gate
    warns on them while turning one is still cheap (#954)."""
    judged = {f["ref"] for f in facts}
    out = []
    for r in sorted(model.footprints):
        if not CONNECTOR_REF.match(r) or r in judged:
            continue
        f = model.footprints[r]
        gap = edge_gap_mm(model, f)
        if gap is not None and gap <= EDGE_NEAR_MM:
            c = f.center_abs()
            out.append({"ref": r, "footprint": _name(f.fpid),
                        "pos": [checklib.rnd(c[0]), checklib.rnd(c[1])],
                        "edge_gap_mm": checklib.rnd(gap, 2)})
    return out


def violations(model, pcb: Path | None = None,
               placement: dict | None = None,
               parts: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(violations, facts) for every mating connector on `model`
    (a placelib.PlaceModel); `placement` is constraints.json's placement
    block, read for its declared mates; `parts` is the board's parts dir
    (parts.json rows for classify, extractions for mouth_reading)."""
    table = load_table()
    pcb = Path(pcb or model.path)
    outline = model.outline
    fps = model.footprints
    mates, out = declared_mates(placement, fps)
    facts: list[dict] = []
    fam_of = {r: family_of(f.fpid, table) for r, f in fps.items()}
    matched_on = {r: "footprint" for r, fam in fam_of.items() if fam}
    need = any(fam_of.values()) or any(
        CONNECTOR_REF.match(r) for r, fam in fam_of.items() if fam is None)
    metas = board_meta(pcb) if pcb.is_file() and need else {}
    rows = load_parts(parts)
    for r, fam in fam_of.items():
        if fam is None and CONNECTOR_REF.match(r):
            fam_of[r], matched_on[r] = classify(
                r, fps[r].fpid, (metas.get(r) or {}).get("props"),
                rows.get(r), table)
    for r in mates:
        if fam_of[r] is None:
            fam_of[r] = {**table["defaults"], **DECLARED_FAMILY}
            matched_on[r] = "declared mate"
    models = {r: m["model"] for r, m in metas.items() if m["model"]}

    def mouth(ref, f, fam, fact):
        props = (metas.get(ref) or {}).get("props") or {}
        lcsc = (props.get("LCSC") or props.get("LCSC Part")
                or (rows.get(ref) or {}).get("lcsc") or "").strip().upper()
        d, how = mouth_reading(f, table, mechanical_of(parts, lcsc))
        fact["mouth_from"] = how["from"]
        if "table" in how:
            out.append(checklib.violation(
                "place", "warning", f.center_abs(), None, None, [ref],
                f"{ref} ({fam['family']}): its datasheet extraction "
                f"({lcsc}) puts the contacts at the {how['contacts_at']}, so "
                f"the mouth is local {d}; the {how['table_from']} reading "
                f"says {how['table']}. Going by the extraction - check it on "
                "the drawing and fix whichever is wrong", SOURCE,
                kind="mating_mouth_disagrees", connector=ref,
                mouth_local=d, table_mouth_local=how["table"],
                table_from=how["table_from"]))
        return d, how.get("contacts_at")

    # the name says vertical but the copper clamps the board edge: horizontal
    entry_of = {r: ("horizontal" if fam["entry"] == "vertical"
                    and straddles(metas.get(r)) else fam["entry"])
                for r, fam in fam_of.items() if fam is not None}

    for ref in sorted(fps):
        f, fam = fps[ref], fam_of[ref]
        if fam is None:
            continue
        body = f.extents_abs()
        meta = metas.get(ref)
        entry = entry_of[ref]
        fact = {"ref": ref, "family": fam["family"], "entry": entry,
                "matched_on": matched_on.get(ref)}
        if entry != fam["entry"]:
            fact["entry_from"] = "pads on both outer layers"
        facts.append(fact)
        others = [o for r, o in fps.items() if r != ref and o.side == f.side]

        mate = mates.get(ref)
        if mate is not None and entry != "horizontal":
            fact["entry_from"] = "declared mate"
            entry = fact["entry"] = "horizontal"
        if mate is not None:
            d, contacts = mouth(ref, f, fam, fact)
            u = _to_abs_dir(f, d) if d is not None else None
            if d is None:
                out.append(checklib.violation(
                    "place", "warning", f.center_abs(), None, None, [ref],
                    f"{ref} ({fam['family']}): cannot read its mouth "
                    "direction from the pad layout, so its declared "
                    "corridor is checked for parts only - add a `mouth` "
                    "entry to reference/connector_mating.yaml", SOURCE,
                    kind="mating_direction_unknown", connector=ref))
            _onboard(ref, fam, f, d, u, body, others, mate, fact, out)
            span = model_span_mm(f, d, meta, pcb, contacts) if d else None
            if model_reversed(span, contacts):
                out.append(_reversed(ref, fam, f.center_abs(), u, span,
                                    contacts))
            continue
        if entry == "horizontal":
            d, contacts = mouth(ref, f, fam, fact)
            if d is None:
                fact["mouth"] = None
                out.append(checklib.violation(
                    "place", "warning", f.center_abs(), None, None, [ref],
                    f"{ref} ({fam['family']}): cannot read its mouth "
                    "direction from the pad layout - add a `mouth` entry "
                    "to reference/connector_mating.yaml", SOURCE,
                    kind="mating_direction_unknown", connector=ref))
                continue
            u = _to_abs_dir(f, d)
            gap = _edge_gap(outline, body, u)
            gaps = {k: _edge_gap(outline, body, a) for k, a in AXES.items()}
            nearest = min(gaps, key=gaps.get)
            facing = _snap(*u)
            fact.update(mouth=facing, gap_mm=checklib.rnd(gap, 2),
                        nearest_edge=nearest)
            c = f.center_abs()
            # brief: "A connector whose mating direction is horizontal must
            # face the nearest board edge, with its mouth at or over that
            # edge." Mouth more than the tolerance short of the edge it
            # faces -> fail; inward when that edge is not the nearest one.
            if gap > fam["mouth_tol_mm"]:
                inward = nearest != facing
                why = (f"its nearest edge is {nearest} "
                       f"({max(gaps[nearest], 0):.1f} mm) but its mouth "
                       f"faces {facing}, into {gap:.1f} mm of board"
                       if inward else
                       f"its mouth is {gap:.1f} mm inside the {facing} edge "
                       f"(limit {fam['mouth_tol_mm']} mm)")
                out.append(checklib.violation(
                    "place", "error", c, None, None, [ref],
                    f"{ref} ({fam['family']}) cannot be mated: {why}",
                    SOURCE,
                    kind="mating_faces_inward" if inward
                    else "mating_mouth_inset",
                    connector=ref, mouth=facing, nearest_edge=nearest,
                    gap_mm=checklib.rnd(gap, 2)))
            span = model_span_mm(f, d, meta, pcb, contacts)
            fact["model_span_mm"] = None if span is None \
                else [checklib.rnd(v, 2) for v in span]
            if model_reversed(span, contacts):
                out.append(_reversed(ref, fam, c, u, span, contacts))
            zone = _zone(body, u, gap, fam["plug_mm"], fam["grip_mm"])
            blockers = sorted(o.ref for o in others
                              if o.extents_abs().intersects(zone)
                              and o.precise_extents_abs().intersection(
                                  zone).area > EPS_AREA)
            fact["blocked_by"] = blockers
            if blockers:
                z = zone.centroid
                out.append(checklib.violation(
                    "place", "error", (z.x, z.y), None, None,
                    [ref, *blockers],
                    f"{ref} ({fam['family']}): "
                    f"{', '.join(blockers)} sit in the plug's insertion zone "
                    f"in front of its mouth ({fam['plug_mm']} mm plug + "
                    f"{fam['grip_mm']} mm grip)", SOURCE,
                    kind="mating_zone_blocked", connector=ref,
                    mouth=facing, blocked_by=blockers))
        else:
            zone = body.buffer(fam["finger_mm"])
            tall, unknown = [], []
            for o in others:
                if entry_of.get(o.ref) == "vertical":
                    continue
                if o.precise_extents_abs().intersection(zone).area <= EPS_AREA:
                    continue
                h = part_height(o, models.get(o.ref), pcb)
                if h is None:
                    unknown.append(o.ref)
                elif h > fam["tall_mm"]:
                    tall.append((o.ref, h))
            fact.update(tall_nearby=[r for r, _ in tall],
                        height_unknown=sorted(unknown))
            if tall:
                names = ", ".join(f"{r} ({h:.1f} mm)" for r, h in sorted(tall))
                out.append(checklib.violation(
                    "place", "error", f.center_abs(), None, None,
                    [ref, *(r for r, _ in tall)],
                    f"{ref} ({fam['family']}): {names} within "
                    f"{fam['finger_mm']} mm - taller than {fam['tall_mm']} mm "
                    "leaves no room for the plug housing and a finger",
                    SOURCE, kind="mating_zone_blocked", connector=ref,
                    blocked_by=sorted(r for r, _ in tall)))
    return out, facts
