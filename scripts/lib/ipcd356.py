"""ipcd356.py - IPC-D-356 netlist and the Gerber netlist compare.

A bare-board tester (flying probe, bed of nails) checks every board against
an IPC-D-356 netlist: continuity inside each net, isolation between nets.
When none is supplied the fab extracts one from the Gerbers, so a Gerber
export defect is "tested good". CAM tools (Valor NPI, CAM350) do the same
compare before release: build the connectivity of the copper the fab will
actually etch and drill, and compare it with the design's netlist.

    short  one copper island (joined through plated holes) holds the test
           points of more than one D-356 net
    open   one D-356 net's test points fall in more than one island, or a
           test point lands on no copper at all

The netlist is kicad-cli's (`pcb export ipcd356`, IPC-D-356A fixed columns):
317 = plated through point (pads and vias), 327 = SMD point, 367 = unplated
hole (never a conductor), 999 = end. KiCad writes UNITS CUST 0 (inch,
0.0001 in) and Y up; this module returns board space (mm, Y down), the same
frame gerblib uses. Access A00 = both sides, A01 = top, A<n> = copper layer n
(KiCad puts a bottom SMD pad at A<layer count>).

kicad-cli keeps the LAST 14 characters of a long net name, so two
"unconnected-(...-PAD5)" nets can share one D-356 name. A tester would then
expect continuity between them and fail a good board; `resolve()` maps each
point back to the board's own net (pad ref+number, via position) and
reports the clash.

Public API:
    parse(text) -> D356
    resolve(d356, bg) -> list[name clash dict]   (sets point.board_net)
    net_tie_classes(pcb_text, bg) -> {net: class id}
    net_tie_refs(pcb_text) -> {ref, ...}
    jumper_groups(pcb_text) -> [(ref, (pin, ...)), ...]
    compare(fab, d356, ties=None, jumpers=None, tie_refs=None)
        -> {"shorts": [...], "opens": [...], "jumper_splits": [...],
            "tie_joins": [...], "tie_opens": [...], ...}
    split_jumper_islands(text, splits) -> (text, renames)
    merge_net_ties(text, joins) -> (text, renames)

A footprint's jumper pads (`jumper_pad_groups`, or
`duplicate_pad_numbers_are_jumpers`) join copper inside the part: a
flow-through ESD diode's pins 1 and 6 carry one net, but on the bare board
they are two islands. kicad-cli names both islands alike, so a tester would
read the net as open (#962). compare() reports such a net under
`jumper_splits`, as an open only while its islands share a D-356 name;
split_jumper_islands() gives each island its own name, which fab_export
writes into the shipped netlist.

A net tie (`net_tie_pad_groups`: a Bridged solder jumper, a NetTie) is the
other way round: the footprint's own copper joins two nets on purpose, and
kicad-cli names each net apart, so a tester reads the island as a short.
compare() lists each island whose nets one tie joins under `tie_joins`, and
as a short (`tie`) while it carries more than one D-356 name;
merge_net_ties() gives those names one, which fab_export writes into the
shipped netlist. A tie whose nets the copper leaves in more than one piece
(the package is older than the footprint's bridge, or the bridge is off
copper) is listed under `tie_opens`: the netlist names those nets apart, so
the tester can't see it.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from shapely.geometry import Point
from shapely.strtree import STRtree

NO_NET = "N/C"

# Mils of the tester's world. CUST 0 / CUST 2: inch, 0.0001 in per count;
# CUST 1: mm, 0.001 mm per count; SI: mm, 0.001 mm.
_UNIT_MM = {"CUST 0": 0.00254, "CUST 2": 0.00254, "CUST 1": 0.001,
            "SI": 0.001, "ENGLISH": 0.00254}

_TAIL = re.compile(
    r"(?:D(?P<hole>\d{4})(?P<plated>[PU]))?\s*A(?P<access>\d{2})"
    r"X(?P<x>[+-]\s*\d+)Y(?P<y>[+-]\s*\d+)"
    r"(?:X(?P<sx>\d{4}))?(?:Y(?P<sy>\d{4}))?(?:R(?P<rot>\d{3}))?")
_ALIAS = re.compile(r"^P\s+(NNAME\d+)\s+(\S.*?)\s*$")
_UNITS = re.compile(r"^P\s+UNITS\s+(.+?)\s*$")

# Test point to copper: KiCad writes the pad's position, which is inside its
# own flash for every pad shape. 1 um covers coordinate round-off (the
# format resolves 2.54 um); a point further out falls back to the nearest
# island within half the pad's smaller side.
_HIT_MM = 0.001


@dataclass
class TestPoint:
    net: str
    ref: str
    pin: str
    x: float
    y: float
    code: str                   # "317" | "327" | "367"
    access: int                 # 0 both, 1 top, n = copper layer n
    hole_mm: float | None = None
    plated: bool | None = None
    size: tuple[float, float] = (0.0, 0.0)
    board_net: str | None = None
    line: int = -1              # index in the text's lines

    @property
    def key(self) -> str:
        """The net the compare groups by: the board's own net when resolve()
        found one, else the D-356 name."""
        return self.board_net or self.net

    @property
    def label(self) -> str:
        return f"{self.ref}-{self.pin}" if self.pin else self.ref

    def as_dict(self) -> dict:
        return {"ref": self.ref, "pin": self.pin, "x": round(self.x, 4),
                "y": round(self.y, 4), "net": self.key}


@dataclass
class D356:
    units: str = "CUST 0"
    points: list = field(default_factory=list)
    aliases: dict = field(default_factory=dict)

    def nets(self) -> set[str]:
        return {p.key for p in self.points if p.key != NO_NET}


class D356Error(ValueError):
    pass


def parse(text: str) -> D356:
    """IPC-D-356(A) text -> D356 (board space, mm)."""
    d = D356()
    for raw in text.splitlines():
        if (m := _UNITS.match(raw)):
            d.units = m.group(1).upper()
        elif (m := _ALIAS.match(raw)):
            d.aliases[m.group(1)] = m.group(2)
    scale = _UNIT_MM.get(d.units)
    if scale is None:
        raise D356Error(f"IPC-D-356 units {d.units!r} not understood")
    for n_line, raw in enumerate(text.splitlines()):
        code = raw[:3]
        if code not in ("317", "327", "367"):
            continue
        line = raw.ljust(80)
        net = line[3:17].strip()
        net = d.aliases.get(net, net)
        ref = line[20:26].strip()
        pin = line[27:31].strip() if line[26] == "-" else ""
        m = _TAIL.search(line, 31)
        if m is None:
            raise D356Error(f"IPC-D-356 record not understood: {raw!r}")
        hole = m.group("hole")
        d.points.append(TestPoint(
            net=net or NO_NET, ref=ref, pin=pin,
            x=int(m.group("x").replace(" ", "")) * scale,
            y=-int(m.group("y").replace(" ", "")) * scale,
            code=code, access=int(m.group("access")),
            hole_mm=int(hole) * scale if hole else None,
            plated=(m.group("plated") == "P") if hole else None,
            size=(int(m.group("sx") or 0) * scale,
                  int(m.group("sy") or 0) * scale),
            line=n_line))
    if not d.points:
        raise D356Error("IPC-D-356 file holds no test points")
    return d


# ------------------------------------------------------------ board side

def resolve(d: D356, bg) -> list[dict]:
    """Point each test point at the board's own net: a pad by ref+number, a
    VIA by position. Returns the D-356 names that stand for more than one
    board net (kicad-cli's 14-character truncation)."""
    pads = {}
    by_ref = defaultdict(list)
    for p in bg.pads_of():
        pads[(p.ref, str(p.number))] = p.net
        by_ref[p.ref].append(p)
    vias = [(v.at, v.net) for v in bg.vias_of()]
    via_tree = STRtree([Point(a) for a, _ in vias]) if vias else None
    by_name: dict[str, set[str]] = defaultdict(set)
    for tp in d.points:
        net = None
        if tp.ref == "VIA" and via_tree is not None:
            i = via_tree.nearest(Point(tp.x, tp.y))
            (vx, vy), vnet = vias[int(i)]
            if abs(vx - tp.x) < 0.01 and abs(vy - tp.y) < 0.01:
                net = vnet
        elif (tp.ref, tp.pin) in pads:
            net = pads[(tp.ref, tp.pin)]
        else:
            # the pin field holds 4 characters ("A1-B12" -> "A1-B"): the
            # ref's pad under the point whose number starts with it
            for p in by_ref.get(tp.ref, ()):
                if str(p.number).startswith(tp.pin) \
                        and abs(p.center[0] - tp.x) < 0.01 \
                        and abs(p.center[1] - tp.y) < 0.01:
                    net = p.net
                    break
        if net:
            tp.board_net = net
            if tp.net != NO_NET:
                by_name[tp.net].add(net)
    return [{"d356_net": name, "board_nets": sorted(nets)}
            for name, nets in sorted(by_name.items()) if len(nets) > 1]


def _sexp_end(text: str, start: int) -> int:
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


_NET_TIE = re.compile(r"\(net_tie_pad_groups((?:\s+\"[^\"]*\")+)\s*\)")
_REF = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')


def net_tie_classes(pcb_text: str, bg) -> dict[str, int]:
    """Nets a net-tie footprint joins on purpose -> one class id per joined
    group. A short whose nets all share a class is the design, not a defect."""
    parent: dict[str, str] = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for m in re.finditer(r"\(footprint\s", pcb_text):
        block = pcb_text[m.start():_sexp_end(pcb_text, m.start())]
        tie = _NET_TIE.search(block)
        ref = _REF.search(block)
        if not tie or not ref:
            continue
        nets = {str(p.number): p.net for p in bg.pads_of(ref=ref.group(1))}
        for group in re.findall(r'"([^"]*)"', tie.group(1)):
            members = {nets.get(n.strip()) for n in group.split(",")}
            members.discard(None)
            members = sorted(members)
            for other in members[1:]:
                parent[find(other)] = find(members[0])
    roots = sorted({find(n) for n in parent})
    return {n: roots.index(find(n)) for n in parent}


def net_tie_refs(pcb_text: str) -> set[str]:
    """The refs of the footprints that carry `net_tie_pad_groups`."""
    out = set()
    for m in re.finditer(r"\(footprint\s", pcb_text):
        block = pcb_text[m.start():_sexp_end(pcb_text, m.start())]
        ref = _REF.search(block)
        if ref and _NET_TIE.search(block):
            out.add(ref.group(1))
    return out


_JUMPER = re.compile(r"\(jumper_pad_groups((?:\s*\((?:\s*\"[^\"]*\")+\s*\))+)\s*\)")
_DUP_JUMPER = re.compile(r"\(duplicate_pad_numbers_are_jumpers\s+yes\s*\)")
_PAD_NUM = re.compile(r'\(pad\s+"([^"]*)"')
_DNP = re.compile(r"\(attr\b[^)]*\bdnp\b|\(dnp\s+yes\s*\)")
_JUMPER_NAME = re.compile(r"~J\d+$")


def jumper_groups(pcb_text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Pads a footprint joins inside the part -> [(ref, pad numbers)]:
    each `jumper_pad_groups` group, and with
    `duplicate_pad_numbers_are_jumpers yes` each number used by more than
    one pad (a group of one: its pads share the number).

    Left out, so a split there stays an open: a do-not-populate part (on
    the assembled board nothing joins its pads), and a group whose pin
    numbers, cut to D-356's 4 characters, match another pad of the part."""
    if ("jumper_pad_groups" not in pcb_text
            and "duplicate_pad_numbers_are_jumpers" not in pcb_text):
        return []
    out = []
    for m in re.finditer(r"\(footprint\s", pcb_text):
        block = pcb_text[m.start():_sexp_end(pcb_text, m.start())]
        ref = _REF.search(block)
        if not ref or _DNP.search(block):
            continue
        nums = _PAD_NUM.findall(block)
        jg = _JUMPER.search(block)
        if jg:
            for group in re.findall(r"\(((?:\s*\"[^\"]*\")+)\s*\)", jg.group(1)):
                pins = tuple(re.findall(r'"([^"]*)"', group))
                cut = {p[:4] for p in pins}
                if len(pins) > 1 and not any(
                        n[:4] in cut for n in nums if n not in pins):
                    out.append((ref.group(1), pins))
        if _DUP_JUMPER.search(block):
            for num in sorted({n for n in nums if n and nums.count(n) > 1}):
                if not any(n[:4] == num[:4] for n in nums if n != num):
                    out.append((ref.group(1), (num,)))
    return out


# ------------------------------------------------------------ copper side

class _DSU:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        p = self.p
        while p.setdefault(a, a) != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


_FILE_FN = re.compile(r"TF\.FileFunction,(?:Plated|NonPlated),(\d+),(\d+)", re.I)


def drill_span(path, n_layers: int) -> tuple[int, int]:
    """The copper layers (1-based, physical order) a drill file's holes
    join: KiCad writes `TF.FileFunction,Plated,<from>,<to>,<PTH|Blind|
    Buried>` in each file's header. No header = the full stack."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        head = ""
    m = _FILE_FN.search(head)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return max(a, 1), min(b, n_layers)
    return 1, n_layers


def _tie_opens(nets_in: dict, roots_of: dict, ties: dict[str, int],
               tie_refs: set) -> list[dict]:
    """Net ties whose nets the shipped copper leaves in more than one
    piece: the package lacks the bridge the board's footprint draws (a
    package older than the board, a bridge moved off copper). The netlist
    names those nets apart, so a tester passes the board with the tie open."""
    if not ties:
        return []
    joined = _DSU()
    present: dict = defaultdict(set)          # class -> its nets on copper
    for by_net in nets_in.values():
        by_cls: dict = defaultdict(list)
        for nm in by_net:
            if nm in ties:
                by_cls[ties[nm]].append(nm)
        for c, nets in by_cls.items():
            present[c].update(nets)
            for nm in nets:
                joined.union(nets[0], nm)
    out = []
    for c, nets in sorted(present.items()):
        parts: dict = defaultdict(list)
        for nm in sorted(nets):
            parts[joined.find(nm)].append(nm)
        if len(parts) < 2:
            continue
        groups = sorted(parts.values())
        # where to look: the tie's own pads, the bridge between them, when
        # both pieces hold one; else the nearest points of the pieces
        pts = {nm: [p for tps in roots_of[nm].values() for p in tps]
               for nm in nets}
        a = [p for nm in groups[0] for p in pts[nm]]
        b = [p for g in groups[1:] for nm in g for p in pts[nm]]
        ta = [p for p in a if p.ref in tie_refs]
        tb = [p for p in b if p.ref in tie_refs]
        if ta and tb:
            a, b = ta, tb
        if len(a) > len(b):
            a, b = b, a
        tree = STRtree([Point(p.x, p.y) for p in b])
        pa, pb = min(((p, b[int(tree.nearest(Point(p.x, p.y)))]) for p in a),
                     key=lambda ab: ((ab[0].x - ab[1].x) ** 2
                                     + (ab[0].y - ab[1].y) ** 2))
        out.append({"nets": sorted(nets), "class": c, "groups": groups,
                    "near": [pa.as_dict(), pb.as_dict()],
                    "at": [round((pa.x + pb.x) / 2, 3),
                           round((pa.y + pb.y) / 2, 3)]})
    return out


def compare(fab, d: D356, ties: dict[str, int] | None = None,
            jumpers: list | None = None, tie_refs=None) -> dict:
    """Connectivity of the shipped copper vs the D-356 netlist. `jumpers`
    (jumper_groups()) are the pads parts join inside: a net whose islands
    meet only through them is a jumper split, not an open, as long as each
    island has a D-356 name of its own; the tester then never probes
    through the part. `tie_refs` (net_tie_refs()) places a tie open at the
    tie's own pads."""
    import gerblib

    ties = ties or {}
    layers = fab.copper_layer_names()
    n = len(layers)
    islands: dict[str, list] = {}
    trees: dict[str, STRtree] = {}
    for name in layers:
        lg = fab.copper(name)
        comps = lg.components() if lg is not None else []
        islands[name] = comps
        trees[name] = STRtree(comps) if comps else None
    dsu = _DSU()
    for name in layers:
        for i in range(len(islands[name])):
            dsu.find((name, i))

    def hits(layer: str, geom) -> list[int]:
        tree = trees.get(layer)
        if tree is None:
            return []
        return [int(i) for i in tree.query(geom, predicate="intersects")]

    # Plated holes join every layer of their span they touch copper on.
    n_holes = 0
    for path in fab.drill_files:
        lo, hi = drill_span(path, n)
        try:
            shapes = [Point(h.x, h.y).buffer(max(h.diameter, 0.01) / 2.0,
                                             quad_segs=8)
                      for h in gerblib.read_drill(path) if h.plated]
            shapes += [s.poly for s in gerblib.read_slots(path) if s.plated]
        except Exception:
            continue
        for disc in shapes:
            n_holes += 1
            found = [(layers[k], i) for k in range(lo - 1, hi)
                     for i in hits(layers[k], disc)]
            for node in found[1:]:
                dsu.union(found[0], node)

    def locate(tp: TestPoint):
        if tp.access == 0:
            order = [layers[0], layers[-1]] + layers[1:-1]
        elif 1 <= tp.access <= n:
            order = [layers[tp.access - 1]]
        else:
            order = [layers[-1]]
        pt = Point(tp.x, tp.y)
        for layer in order:
            got = hits(layer, pt.buffer(_HIT_MM))
            if got:
                return (layer, got[0])
        reach = min((s for s in tp.size if s > 0), default=0.0) / 2.0
        for layer in order:
            tree = trees.get(layer)
            if tree is None or reach <= 0:
                continue
            i = int(tree.nearest(pt))
            if islands[layer][i].distance(pt) <= reach:
                return (layer, i)
        return None

    nets_in: dict = defaultdict(lambda: defaultdict(list))   # root -> net -> tps
    roots_of: dict = defaultdict(lambda: defaultdict(list))  # net -> root -> tps
    no_copper = []
    n_points = 0
    for tp in d.points:
        if tp.code == "367" or tp.key == NO_NET:
            continue
        n_points += 1
        node = locate(tp)
        if node is None:
            no_copper.append(tp)
            continue
        root = dsu.find(node)
        nets_in[root][tp.key].append(tp)
        roots_of[tp.key][root].append(tp)

    def where(root) -> dict:
        layer, i = root
        x0, y0, x1, y1 = islands[layer][i].bounds
        return {"layer": layer, "bbox": [round(v, 3) for v in (x0, y0, x1, y1)]}

    def short(root, by_net, **extra) -> dict:
        # the nearest pair of points from different nets: where to look
        nets = sorted(by_net)
        best = None
        for a in range(len(nets)):
            for b in range(a + 1, len(nets)):
                for pa in by_net[nets[a]]:
                    for pb in by_net[nets[b]]:
                        dd = (pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2
                        if best is None or dd < best[0]:
                            best = (dd, pa, pb)
        _, pa, pb = best
        return {
            "nets": nets,
            "near": [pa.as_dict(), pb.as_dict()],
            "at": [round((pa.x + pb.x) / 2, 3), round((pa.y + pb.y) / 2, 3)],
            "points": {nm: [p.label for p in by_net[nm]][:6] for nm in nets},
            "island": where(root), **extra}

    # D-356 name -> the board nets its resolved points belong to: a name
    # that also stands for a net outside a tie can't take the tie's nets
    name_nets: dict = defaultdict(set)
    for tp in d.points:
        if tp.board_net and tp.net != NO_NET:
            name_nets[tp.net].add(tp.board_net)

    shorts = []
    tie_joins = []
    for root, by_net in nets_in.items():
        by_name: dict = defaultdict(list)
        for tps in by_net.values():
            for p in tps:
                by_name[p.net].append(p)
        if len(by_net) < 2:
            # one board net, but islands named apart (split_jumper_islands)
            # meet in this copper: the tester reads the two names as a short
            if len(by_name) > 1 and any(_JUMPER_NAME.search(n) for n in by_name):
                shorts.append(short(root, by_name, jumper=True))
            continue
        nets = sorted(by_net)
        cls = {ties.get(nm, ("solo", nm)) for nm in nets}
        if len(cls) > 1:
            shorts.append(short(root, by_net))
            continue
        # A net tie joins exactly these nets: the copper is the design. The
        # tester knows the island only by its D-356 names, and reads two
        # names in one island as a short (merge_net_ties gives it one).
        (c,) = cls
        members = {nm for nm, k in ties.items() if k == c}
        names = sorted(by_name)
        mergeable = all(name_nets[nm] <= members for nm in names)
        tie_joins.append({
            "nets": nets, "class": c, "d356_names": names,
            "mergeable": mergeable,
            "n_points": sum(len(t) for t in by_net.values()),
            "island": where(root)})
        if len(names) > 1:
            shorts.append(short(root, by_net, tie=True, d356_names=names,
                                mergeable=mergeable))
    # D-356 keeps 6 characters of a ref
    tie_opens = _tie_opens(nets_in, roots_of, ties,
                           {r[:6] for r in tie_refs or ()})
    # pad label -> the jumper group ids it is in. D-356 keeps 4 characters
    # of a pin number.
    jump_of: dict = defaultdict(set)
    for k, (ref, pins) in enumerate(jumpers or ()):
        for pin in pins:
            jump_of[(ref, pin[:4])].add(k)

    def jumper_joined(by_root) -> bool:
        if not jump_of:
            return False
        dj = _DSU()
        for root, tps in by_root.items():
            dj.find(("r", root))
            for tp in tps:
                for k in jump_of.get((tp.ref, tp.pin), ()):
                    dj.union(("r", root), ("j", k))
        return len({dj.find(("r", r)) for r in by_root}) == 1

    opens = []
    jumper_splits = []
    for net, by_root in roots_of.items():
        if len(by_root) < 2:
            continue
        frags = sorted(by_root.values(), key=len, reverse=True)
        if jumper_joined(by_root):
            names = [sorted({p.net for p in f}) for f in frags]
            shared = sorted(set.intersection(*(set(n) for n in names))) \
                or sorted(set().union(*names))
            own = all(len(set(a) & set(b)) == 0
                      for i, a in enumerate(names) for b in names[i + 1:])
            jumper_splits.append({
                "net": net, "n_fragments": len(frags),
                "fragments": [[p.label for p in f][:8] for f in frags],
                "d356_names": names, "own_names": own,
                "points": [[p.line for p in f] for f in frags]})
            if own:
                continue
            opens.append({
                "net": net, "jumper": True, "shared_names": shared,
                "fragments": [[p.label for p in f][:8] for f in frags],
                "n_fragments": len(frags),
                "at": [round(frags[-1][0].x, 3), round(frags[-1][0].y, 3)]})
            continue
        opens.append({
            "net": net,
            "fragments": [[p.label for p in f][:8] for f in frags],
            "n_fragments": len(frags),
            "at": [round(frags[-1][0].x, 3), round(frags[-1][0].y, 3)]})
    for tp in no_copper:
        opens.append({"net": tp.key, "no_copper": tp.as_dict(),
                      "fragments": [[tp.label]], "n_fragments": 1,
                      "at": [round(tp.x, 3), round(tp.y, 3)]})
    shorts.sort(key=lambda s: s["nets"])
    opens.sort(key=lambda o: (o["net"], "no_copper" in o))
    jumper_splits.sort(key=lambda j: j["net"])
    tie_joins.sort(key=lambda j: j["nets"])
    return {"shorts": shorts, "opens": opens, "jumper_splits": jumper_splits,
            "tie_joins": tie_joins, "tie_opens": tie_opens,
            "n_points": n_points,
            "n_nets": len(roots_of), "n_plated_holes": n_holes,
            "n_islands": {k: len(v) for k, v in islands.items()},
            "fragments_per_net": {net: len(r) for net, r in roots_of.items()}}


JUMPER_SUFFIX = "~J"


def split_jumper_islands(text: str, splits: list[dict]) -> tuple[str, list[dict]]:
    """Give each island of a jumper split its own D-356 net name: the
    largest island keeps the name, each other one gets `<name>~J<k>`
    (cut from the left to the 14-character field, unique in the file).
    `splits` is compare()'s `jumper_splits`; a split whose islands already
    have names of their own is left. -> (new text, renames)."""
    lines = text.splitlines()
    eol = "\r\n" if "\r\n" in text else "\n"
    aliases = {m.group(1): m.group(2) for ln in lines if (m := _ALIAS.match(ln))}
    used = set(aliases.values()) | {
        ln[3:17].strip() for ln in lines if ln[:3] in ("317", "327", "367")}
    renames = []
    for sp in splits:
        if sp.get("own_names"):
            continue
        for frag in sp["points"][1:]:
            base = lines[frag[0]][3:17].strip()
            base = aliases.get(base, base)
            k = 1
            while True:
                sfx = f"{JUMPER_SUFFIX}{k}"
                name = (base[-(14 - len(sfx)):] if len(base) + len(sfx) > 14
                        else base) + sfx
                if name not in used:
                    break
                k += 1
            used.add(name)
            for i in frag:
                ln = lines[i].ljust(17)
                lines[i] = ln[:3] + f"{name:<14}" + ln[17:]
            renames.append({"net": sp["net"], "from": base, "to": name,
                            "points": len(frag)})
    return eol.join(lines) + (eol if text.endswith("\n") else ""), renames


_RECORD = ("317", "327", "367")


def merge_net_ties(text: str, joins: list[dict]) -> tuple[str, list[dict]]:
    """Give the D-356 names of each net-tie island (compare()'s
    `tie_joins`) one name, so the tester expects the continuity the tie's
    copper makes. Of the names an island carries, the one with the most
    records in the file stays (then the first in sort order), and every
    record of the others takes it, wherever it lies: the points of one net
    keep one name, so an open in it is still an open. A join whose names
    also stand for a net outside the tie (`mergeable` false: a 14-character
    clash) is left. -> (new text, renames)."""
    lines = text.splitlines()
    eol = "\r\n" if "\r\n" in text else "\n"
    aliases = {m.group(1): m.group(2) for ln in lines if (m := _ALIAS.match(ln))}
    recs: dict[str, list[int]] = defaultdict(list)
    raw_of: dict[str, str] = {}
    for i, ln in enumerate(lines):
        if ln[:3] not in _RECORD:
            continue
        raw = ln.ljust(17)[3:17]
        name = aliases.get(raw.strip(), raw.strip())
        recs[name].append(i)
        raw_of.setdefault(name, raw)
    dsu = _DSU()
    joins = [j for j in joins if j.get("mergeable", True)
             and len([n for n in j["d356_names"] if n in recs]) > 1]
    for j in joins:
        names = [n for n in j["d356_names"] if n in recs]
        for nm in names[1:]:
            dsu.union(names[0], nm)
    groups: dict = defaultdict(set)
    nets_of: dict = defaultdict(set)
    for j in joins:
        names = [n for n in j["d356_names"] if n in recs]
        root = dsu.find(names[0])
        groups[root].update(names)
        nets_of[root].update(j["nets"])
    renames = []
    for root, members in sorted(groups.items(), key=lambda kv: sorted(kv[1])):
        win = min(members, key=lambda nm: (-len(recs[nm]), nm))
        for nm in sorted(members - {win}):
            for i in recs[nm]:
                ln = lines[i].ljust(17)
                lines[i] = ln[:3] + raw_of[win] + ln[17:]
            renames.append({"nets": sorted(nets_of[root]), "from": nm,
                            "to": win, "points": len(recs[nm])})
    return eol.join(lines) + (eol if text.endswith("\n") else ""), renames
